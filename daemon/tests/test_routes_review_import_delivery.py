"""Review, external import and delivery routes (router plan T74) on the committed Blender packet `room_a`.

`EchoEngine` hands back the PNG egress sent it (optionally shifted), so the gates score real pixels: an
unshifted echo ends DONE, a 12 px shift ends GATE_FAIL on camera drift. Clip and previs takes are made on the
run table directly (the motion engines need vendors); ffmpeg-dependent tests skip when the tool or the encoder
is absent. No client data is read.
"""

from __future__ import annotations

import base64
import json
import re
import struct
import subprocess
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from blake3 import blake3
from fastapi.testclient import TestClient

from plate_daemon import lineage
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.db import tx
from plate_daemon.delivery import guard, tools
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.gate_reports import Service
from plate_daemon.ledger import Ledger
from plate_daemon.machine import annotate, create_run, run_events, transition
from plate_daemon.models import QUOTE_TTL, Quote, RunCreate
from plate_daemon.outbound_log import OUTBOUND_JSONL
from plate_daemon.pipeline import decode_png16
from plate_daemon.quotes import insert_quote
from plate_daemon.states import RunState
from plate_daemon.timeutil import utcnow

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
FAKE_ROUTE = {"engine_id": "fake", "provider": "fake", "region": "local"}
REASON = "client signed off the drift"
S = RunState

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
    c = TestClient(create_app(settings, engine or EchoEngine()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict, *, nda: bool = False) -> str:
    """Project + room_a ingested + Plate -> Edit `e` on the fake engine. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        {"id": "e", "kind": "Edit", "label": "e", "params": {"engine": "fake"}},
    ]
    edges = [{"id": "p->e", "source": "p", "target": "e"}]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _run(c: TestClient, auth: dict) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


def _records(settings: Settings, kind: str, run_id: str) -> list[dict]:
    led = Ledger(settings.projects_dir / PROJECT)
    assert led.verify() == (True, None)
    return [r for r in led.records() if r["kind"] == kind and r.get("run_id") == run_id]


def _plate16() -> np.ndarray:
    """room_a's display master as uint16 RGB."""
    img = cv2.imread(str(ROOM_A / "display.tif"), cv2.IMREAD_UNCHANGED)
    assert img is not None and img.dtype == np.uint16
    return np.ascontiguousarray(img[..., :3][..., ::-1])


def _write_png16(path: Path, rgb16: np.ndarray) -> Path:
    assert cv2.imwrite(str(path), np.ascontiguousarray(rgb16[..., ::-1]))
    return path


def _png_widths(zip_path: Path) -> dict[str, int]:
    with zipfile.ZipFile(zip_path) as zf:
        return {n: struct.unpack(">I", zf.read(n)[16:20])[0] for n in zf.namelist() if n.endswith(".png")}


def _import(c: TestClient, auth: dict, **body):
    return c.post(f"/api/v0/projects/{PROJECT}/import", json=body, headers=auth)


def _forge(
    c: TestClient,
    packet_id: str,
    out_key: str,
    *,
    node: str,
    inputs: dict | None = None,
    state: RunState = S.DONE,
) -> str:
    """A run of `node` whose take is `out_key` in the project's CAS, walked through the state table to
    `state` with a stored gate report (what a motion or previs run leaves behind)."""
    index = c.app.state.index
    project_dir = c.app.state.pipeline.project_dir(PROJECT)
    now = utcnow()
    with index.project(PROJECT) as conn:
        spec = RunCreate(
            project_id=PROJECT,
            packet_id=packet_id,
            node_id=node,
            inputs={"take": out_key, **(inputs or {})},
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
        with tx(conn):
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
        transition(conn, run.id, S.GATING, actor="test", normalized={"outputs": [out_key], **(inputs or {})})
        verdict = "pass" if state is S.DONE else "fail"
        report = {
            "gate_version": "0.1.0",
            "metric_code_hash": "ab" * 32,
            "threshold_set_id": "test",
            "verdict": verdict,
            "checks": [{"metric_id": "texture.lapvar", "value": 412.0, "status": verdict}],
        }
        path = Service(project_dir).write(run.id, report)
        report["report_path"] = Path(path).relative_to(project_dir).as_posix()
        transition(conn, run.id, state, actor="test", gate_report=report)
    return run.id


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


def _clip(path: Path, seconds: int = 2) -> Path:
    """A small untagged H.264 test clip (24 fps)."""
    tools.run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=128x72:rate=24:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(path),
        ]
    )
    return path


# ---------------------------------------------------------------------------- review


@needs_room_a
def test_review_of_a_done_run_writes_a_zip_with_the_page_and_no_png_over_the_cap(sync_settings, auth):
    with _client(sync_settings) as c:
        _setup(c, auth)
        run = _run(c, auth)
        assert run["state"] == "DONE", run.get("error")

        r = c.post(f"/api/v0/projects/{PROJECT}/review", json={"run_ids": [run["id"]]}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        zip_path = Path(body["zip_path"])
        assert zip_path.is_file() and zip_path.parent == sync_settings.projects_dir / PROJECT / "review"
        assert body["bytes"] == zip_path.stat().st_size and body["takes"] == 1
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert any(n.endswith("/index.html") for n in names)
        widths = _png_widths(zip_path)
        assert len(widths) == 2 and max(widths.values()) <= 2048
        assert PROJECT not in " ".join(names)  # file names carry the take number only

        r = c.post(
            f"/api/v0/projects/{PROJECT}/review",
            json={"run_ids": [run["id"]], "max_width": 256, "title": "Lobby"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        assert max(_png_widths(Path(r.json()["zip_path"])).values()) <= 256

        for bad in ({"run_ids": [run["id"]], "max_width": 4096}, {"run_ids": [run["id"]], "watermark": ""}):
            assert c.post(f"/api/v0/projects/{PROJECT}/review", json=bad, headers=auth).status_code == 422
        r = c.post(f"/api/v0/projects/{PROJECT}/review", json={"run_ids": ["run_nope"]}, headers=auth)
        assert r.status_code == 404 and r.json()["run_id"] == "run_nope"
        r = c.post("/api/v0/projects/absent/review", json={"run_ids": [run["id"]]}, headers=auth)
        assert r.status_code == 404
        assert c.post(f"/api/v0/projects/{PROJECT}/review", json={"run_ids": [run["id"]]}).status_code == 401


@needs_room_a
def test_review_refuses_a_previs_take(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        key = c.app.state.cas_for(PROJECT).put_bytes((ROOM_A / "display.tif").read_bytes(), "tif")
        previs_id = _forge(c, packet_id, key, node="motion", inputs={"previs": True})
        r = c.post(f"/api/v0/projects/{PROJECT}/review", json={"run_ids": [previs_id]}, headers=auth)
        assert r.status_code == 409, r.text
        assert r.json()["error"] == "not_reviewable" and r.json()["code"] == "PREVIS_NOT_DELIVERABLE"
        assert not (sync_settings.projects_dir / PROJECT / "review").exists() or not any(
            (sync_settings.projects_dir / PROJECT / "review").glob("*.zip")
        )


@needs_room_a
def test_review_refuses_a_gate_fail_take_until_it_is_overridden(sync_settings, auth):
    with _client(sync_settings, EchoEngine(shift_px=12)) as c:
        _setup(c, auth)
        run = _run(c, auth)
        assert run["state"] == "GATE_FAIL"
        body = {"run_ids": [run["id"]]}
        r = c.post(f"/api/v0/projects/{PROJECT}/review", json=body, headers=auth)
        assert r.status_code == 409 and r.json()["code"] == "GATE_FAIL", r.text
        r = c.post(f"/api/v0/runs/{run['id']}/override", json={"reason": REASON}, headers=auth)
        assert r.status_code == 200, r.text
        r = c.post(f"/api/v0/projects/{PROJECT}/review", json=body, headers=auth)
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------- external import


@needs_room_a
def test_import_of_the_plate_itself_is_done_and_every_record_says_imported(sync_settings, auth):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        src = ROOM_A / "display.tif"
        r = _import(
            c, auth, path=str(src), packet_id=packet_id, source="manual", cost_usd=0.25, note="retouch"
        )
        assert r.status_code == 201, r.text
        run = r.json()
        run_id = run["id"]
        assert run["state"] == "DONE", run.get("error")
        assert run["exportable"] is True and run["attempt"] == 1 and run["node_id"] is None
        assert run["route"]["engine_id"] == "import:manual" and run["route"]["custody_grade"] == "C0"
        assert run["quote"]["price_usd"] == 0.25 and run["quote"]["price_status"] == "declared"
        raw_key = blake3(src.read_bytes()).hexdigest()
        imported = run["inputs"]["imported"]
        assert imported == {
            "path_name": "display.tif",
            "source": "manual",
            "note": "retouch",
            "blake3": raw_key,
        }
        assert run["normalized"]["raw_key"] == raw_key == run["result_key"]
        assert run["inputs"]["mask"]["source"] == "policy"

        cas = c.app.state.cas_for(PROJECT)
        out_key = run["outputs"][0]
        assert out_key != raw_key and cas.find(raw_key).suffix == ".tif"
        assert np.array_equal(decode_png16(cas.read_bytes(out_key)), _plate16())
        lin = lineage.read(cas.find(out_key))
        assert lin is not None and lin.run_id == run_id and lin.packet_id == packet_id
        assert lin.engine_id == "import:manual" and lin.gate_report == f"gates/{run_id}.1.json"

        gate = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()
        assert gate["verdict"] in ("pass", "advisory") and gate["n"] == 1
        assert gate["metrics"]["registration"]["camera_drift"] is False

        (quoted,) = _records(sync_settings, "quoted", run_id)
        assert quoted["data"]["imported"] is True and quoted["cost_usd_quoted"] == 0.25
        assert quoted["engine_id"] == "import:manual"
        (approved,) = _records(sync_settings, "approved", run_id)
        assert approved["data"]["imported"] is True and approved["by"] == "human"
        (done,) = _records(sync_settings, "done", run_id)
        assert done["data"]["imported"] is True and done["data"]["outputs"] == [out_key]
        assert _records(sync_settings, "submitted", run_id) == []
        assert not (sync_settings.projects_dir / PROJECT / OUTBOUND_JSONL).exists()  # nothing left the box
        states = [e.to_state for e in _events(c, run_id) if e.kind == "transition"]
        assert states[-1] == "DONE" and "GATING" in states and "SUBMITTED" in states

        # the same file again is the next attempt of the same request, not a duplicate
        r = _import(
            c, auth, path=str(src), packet_id=packet_id, source="manual", cost_usd=0.25, note="retouch"
        )
        assert r.status_code == 201 and r.json()["attempt"] == 2 and r.json()["state"] == "DONE", r.text

        # an imported take is scored again like an Edit take
        r = c.post(f"/api/v0/runs/{run_id}/gate/rescore", json={}, headers=auth)
        assert r.status_code == 200 and r.json()["n"] == 2, r.text


def _events(c: TestClient, run_id: str):
    with c.app.state.index.project(PROJECT) as conn:
        return run_events(conn, run_id)


@needs_room_a
def test_import_of_a_shifted_plate_fails_on_camera_drift_and_exports_after_an_override(
    sync_settings, auth, tmp_path
):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        shifted = _write_png16(tmp_path / "shifted.png", np.roll(_plate16(), 12, axis=1))
        r = _import(c, auth, path=str(shifted), packet_id=packet_id, node_id="e", source="manual")
        assert r.status_code == 201, r.text
        run = r.json()
        run_id = run["id"]
        assert run["state"] == "GATE_FAIL" and run["exportable"] is False and run["node_id"] == "e"
        assert run["subgraph"] is not None and {n["id"] for n in run["subgraph"]["nodes"]} == {"p", "e"}
        gate = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()
        assert gate["verdict"] == "fail"
        assert gate["metrics"]["registration"]["camera_drift"] is True
        shift = next(ch for ch in gate["checks"] if ch["metric_id"] == "registration.shift_px")
        assert shift["status"] == "fail"
        assert any(p["note"] == "camera drift" for p in gate["flagged_points"])
        (gated,) = _records(sync_settings, "gated", run_id)
        assert gated["data"]["imported"] is True and gated["data"]["verdict"] == "fail"

        out = tmp_path / "out"
        r = c.post(
            f"/api/v0/runs/{run_id}/export", json={"out_dir": str(out), "format": "png16"}, headers=auth
        )
        assert r.status_code == 409, r.text
        r = c.post(f"/api/v0/runs/{run_id}/override", json={"reason": REASON}, headers=auth)
        assert r.status_code == 200 and r.json()["exportable"] is True, r.text
        r = c.post(
            f"/api/v0/runs/{run_id}/export", json={"out_dir": str(out), "format": "png16"}, headers=auth
        )
        assert r.status_code == 200, r.text
        lin = lineage.read(Path(r.json()["path"]))
        assert lin is not None and lin.run_id == run_id


@needs_room_a
def test_import_refusals_leave_no_run(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth, nda=True)
        plate = str(ROOM_A / "display.tif")

        r = _import(c, auth, path=plate, packet_id=packet_id, source="higgsfield")
        assert r.status_code == 403, r.text
        assert r.json()["error"] == "custody" and r.json()["grade"] == "D"
        r = _import(c, auth, path=plate, packet_id=packet_id, source="runway")
        assert r.status_code == 403

        r = _import(c, auth, path=plate, packet_id=packet_id, source="midjourney")
        assert r.status_code == 422 and r.json()["error"] == "unknown_source", r.text

        small = _write_png16(tmp_path / "small.png", _plate16()[::2, ::2])
        r = _import(c, auth, path=str(small), packet_id=packet_id, source="manual")
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "size_mismatch"
        assert r.json()["plate"] == [640, 360] and r.json()["file"] == [320, 180]

        junk = tmp_path / "junk.png"
        junk.write_bytes(b"not a png at all")
        r = _import(c, auth, path=str(junk), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_image", r.text
        r = _import(c, auth, path=str(tmp_path / "absent.png"), packet_id=packet_id, source="manual")
        assert r.status_code == 422 and r.json()["error"] == "unreadable_image"
        r = _import(c, auth, path="relative.png", packet_id=packet_id, source="manual")
        assert r.status_code == 422

        r = _import(c, auth, path=plate, packet_id="pkt_absent", source="manual")
        assert r.status_code == 404 and r.json()["error"] == "not_found"
        r = _import(c, auth, path=plate, packet_id=packet_id, node_id="nope", source="manual")
        assert r.status_code == 404 and r.json()["node_id"] == "nope"
        r = c.post(
            "/api/v0/projects/absent/import",
            json={"path": plate, "packet_id": packet_id, "source": "manual"},
            headers=auth,
        )
        assert r.status_code == 404

        assert c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json() == []
        assert not (sync_settings.projects_dir / PROJECT / "runs.jsonl").exists() or not any(
            rec["kind"] == "quoted" for rec in Ledger(sync_settings.projects_dir / PROJECT).records()
        )

        # a manual file is C0: an NDA project takes it
        r = _import(c, auth, path=plate, packet_id=packet_id, source="manual")
        assert r.status_code == 201 and r.json()["state"] == "DONE", r.text


# ---------------------------------------------------------------------------- delivery


@needs_room_a
def test_deliver_a_clip_as_hevc_lands_bt709_tags(sync_settings, auth, tmp_path):
    _need_ffmpeg("libx264", "libx265")
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        key = c.app.state.cas_for(PROJECT).put_file(_clip(tmp_path / "take.mp4"), "mp4")
        run_id = _forge(c, packet_id, key, node="motion")
        out = tmp_path / "delivery"
        r = c.post(
            f"/api/v0/runs/{run_id}/deliver",
            json={"format": "hevc", "out_dir": str(out), "name": "shot_01"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "path": str(out / "shot_01.mp4"),
            "sidecar": str(out / "shot_01.mp4.gate.json"),
            "format": "hevc",
        }
        stream = tools.probe_video(out / "shot_01.mp4")
        assert stream["color_primaries"] == "bt709" and stream["codec_name"] == "hevc"
        sidecar = json.loads(Path(body["sidecar"]).read_text(encoding="utf-8"))
        assert sidecar["lineage"]["run_id"] == run_id and sidecar["gate"]["verdict"] == "pass"
        (exp,) = _records(sync_settings, "export", run_id)
        assert exp["data"]["delivery"] is True and exp["data"]["format"] == "hevc"
        assert c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()["shipped"] is True

        r = c.post(
            f"/api/v0/runs/{run_id}/deliver",
            json={"format": "hevc", "out_dir": str(out), "name": "shot_01"},
            headers=auth,
        )
        assert r.status_code == 409 and r.json()["error"] == "exists", r.text


@needs_room_a
def test_deliver_a_clip_as_prores_exr_frames_and_a_timeline(sync_settings, auth, tmp_path):
    _need_ffmpeg("libx264", "prores_ks")
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        key = c.app.state.cas_for(PROJECT).put_file(_clip(tmp_path / "take.mp4", seconds=1), "mp4")
        run_id = _forge(c, packet_id, key, node="motion")
        out = tmp_path / "delivery"

        def deliver(fmt: str, **extra):
            body = {"format": fmt, "out_dir": str(out), "name": f"shot_{fmt}", **extra}
            return c.post(f"/api/v0/runs/{run_id}/deliver", json=body, headers=auth)

        r = deliver("prores4444")
        assert r.status_code == 200, r.text
        assert tools.probe_video(Path(r.json()["path"]))["codec_name"] == "prores"

        r = deliver("exr_sequence")
        assert r.status_code == 200, r.text
        frames = sorted(out.glob("shot_exr_sequence.*.exr"))
        assert len(frames) == 24 and Path(r.json()["path"]) == out / "shot_exr_sequence.0001.exr"
        assert lineage.read(frames[-1]).run_id == run_id
        assert not list((sync_settings.home / "cache" / "deliver").glob("*"))  # the decoded frames are gone

        r = deliver("fcpxml", timeline={"name": "Reel"})
        assert r.status_code == 200, r.text
        text = Path(r.json()["path"]).read_text(encoding="utf-8")
        assert 'version="1.10"' in text and "Reel" in text
        assert Path(r.json()["sidecar"]).name == "shot_fcpxml.fcpxml.gate.json"

        r = deliver("av1_retag")  # an H.264 take has nothing to re-tag
        assert r.status_code == 422 and r.json()["error"] == "delivery_error", r.text


@needs_room_a
def test_deliver_a_still_as_a_one_frame_exr_sequence_and_refuse_video_formats(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        _setup(c, auth)
        run_id = _run(c, auth)["id"]
        out = tmp_path / "delivery"
        for fmt in ("hevc", "prores4444", "av1_retag", "fcpxml"):
            r = c.post(
                f"/api/v0/runs/{run_id}/deliver", json={"format": fmt, "out_dir": str(out)}, headers=auth
            )
            assert r.status_code == 422 and r.json()["error"] == "not_a_clip", (fmt, r.text)
        r = c.post(
            f"/api/v0/runs/{run_id}/deliver", json={"format": "gif", "out_dir": str(out)}, headers=auth
        )
        assert r.status_code == 422 and r.json()["error"] == "bad_format"
        r = c.post(
            f"/api/v0/runs/{run_id}/deliver", json={"format": "exr_sequence", "out_dir": "rel"}, headers=auth
        )
        assert r.status_code == 422
        r = c.post(
            "/api/v0/runs/run_nope/deliver",
            json={"format": "exr_sequence", "out_dir": str(out)},
            headers=auth,
        )
        assert r.status_code == 404

        r = c.post(
            f"/api/v0/runs/{run_id}/deliver",
            json={"format": "exr_sequence", "out_dir": str(out), "name": "still"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["path"] == str(out / "still.0001.exr") and body["format"] == "exr_sequence"
        assert Path(body["path"]).is_file() and Path(body["sidecar"]) == out / "still.gate.json"
        lin = lineage.read(Path(body["path"]))
        assert lin is not None and lin.run_id == run_id


@needs_room_a
def test_deliver_refuses_a_gate_fail_without_a_reason_and_ledgers_a_typed_one(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine(shift_px=12)) as c:
        _setup(c, auth)
        run_id = _run(c, auth)["id"]
        out = tmp_path / "delivery"
        body = {"format": "exr_sequence", "out_dir": str(out), "name": "drift"}
        r = c.post(f"/api/v0/runs/{run_id}/deliver", json=body, headers=auth)
        assert r.status_code == 409, r.text
        assert r.json()["error"] == "delivery_refused" and r.json()["code"] == "GATE_FAIL"
        r = c.post(f"/api/v0/runs/{run_id}/deliver", json={**body, "override_reason": "short"}, headers=auth)
        assert r.status_code == 409 and r.json()["code"] == "OVERRIDE_REASON_TOO_SHORT"
        assert _records(sync_settings, "override", run_id) == [] and not out.exists()

        r = c.post(f"/api/v0/runs/{run_id}/deliver", json={**body, "override_reason": REASON}, headers=auth)
        assert r.status_code == 200, r.text
        (ovr,) = _records(sync_settings, "override", run_id)
        assert ovr["reason"] == REASON and ovr["by"] == "human"
        (exp,) = _records(sync_settings, "export", run_id)
        assert exp["data"]["override_reason"] == REASON and exp["data"]["delivery"] is True
        assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["override"]["reason"] == REASON
        sidecar = json.loads(Path(r.json()["sidecar"]).read_text(encoding="utf-8"))
        assert sidecar["override"]["reason"] == REASON and sidecar["run_state"] == "GATE_FAIL"


@needs_room_a
def test_deliver_refuses_previs(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        packet_id = _setup(c, auth)
        key = c.app.state.cas_for(PROJECT).put_bytes((ROOM_A / "display.tif").read_bytes(), "tif")
        run_id = _forge(c, packet_id, key, node="motion", inputs={"previs": True})
        r = c.post(
            f"/api/v0/runs/{run_id}/deliver",
            json={"format": "exr_sequence", "out_dir": str(tmp_path / "d"), "override_reason": REASON},
            headers=auth,
        )
        assert r.status_code == 409 and r.json()["code"] == "PREVIS_NOT_DELIVERABLE", r.text
        assert not (tmp_path / "d").exists()
