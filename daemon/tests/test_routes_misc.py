"""The human-in-the-loop and ingestion routes (router plan T38): Tier C and kit ingestion, scrub and verdict, the
human-minute timer, drop-to-resolve and the outbound log.

The pixels worker is never started: Tier C runs in this process (`PLATE_PIXELS_INPROC=1`, stub backends) or
against a fake of the one `httpx.post` the daemon makes to the worker (and, once, a real worker on a socket). Runs that need a DONE take go through the real pipeline
on `fixtures/packets/room_a` with an engine that echoes the PNG it was sent.
"""

from __future__ import annotations

import base64
import json
import shutil
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plate_daemon import lineage, timer, verdicts
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import KINDS, Ledger
from plate_daemon.outbound_log import OutboundLog
from plate_daemon.phash import PhashIndex
from plate_daemon.routes import include_all

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
KIT_FIXTURE = ROOT / "plugins" / "max" / "tests" / "kit_fixture"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, so the gates score real pixels and the run is DONE."""

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


@pytest.fixture
def labels_file(plate_env, monkeypatch) -> Path:
    path = plate_env / "calib" / "labels.jsonl"
    monkeypatch.setenv("PLATE_LABELS", str(path))
    return path


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _project(c: TestClient, auth: dict, project_id: str = PROJECT, nda: bool = False) -> None:
    r = c.post("/api/v0/projects", json={"project_id": project_id, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text


def _done_run(c: TestClient, auth: dict) -> str:
    """room_a through the real pipeline on the echo engine: a DONE run with a gate report and an output."""
    _project(c, auth)
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }
    assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
    assert run["state"] == "DONE", run.get("error")
    return run_id


def _scrub(c: TestClient, auth: dict, run_id: str, quadrants: int = 4, zoom: bool = True):
    return c.post(
        f"/api/v0/runs/{run_id}/scrub", json={"quadrants_visited": quadrants, "zoom_100": zoom}, headers=auth
    )


def _plate_png(path: Path, width: int = 96, height: int = 64) -> Path:
    y, x = np.mgrid[0:height, 0:width]
    img = np.stack([x * 255 // width, y * 255 // height, (x + y) * 255 // (width + height)], axis=-1)
    assert cv2.imwrite(str(path), np.ascontiguousarray(img.astype(np.uint8)[..., ::-1]))
    return path


def test_every_route_module_is_still_included_by_name():
    names = include_all(FastAPI())
    assert {"assets", "events", "misc", "packets", "projects", "runs", "tierc"} <= set(names)
    assert names == sorted(names)


def test_the_ledger_knows_the_new_kinds():
    assert {"verdict", "llm", "scrub"} <= KINDS


# ---------------------------------------------------------------------------- Tier C


def test_tierc_without_a_consent_record_is_428(sync_settings, auth):
    with _client(sync_settings) as c:
        _project(c, auth)
        body = {"plate_path": "/does/not/matter.png", "shot_id": "hero"}
        url = f"/api/v0/projects/{PROJECT}/tierc"
        for extra in ({}, {"consent_record": ""}, {"consent_record": "   "}):
            r = c.post(url, json={**body, **extra}, headers=auth)
            assert r.status_code == 428, r.text
            assert r.json() == {"error": "consent record required"}
        assert c.post(url, json={**body, "consent_record": "x"}).status_code == 401
        assert not (sync_settings.packets_dirs[0] / PROJECT).exists()  # nothing was built


def test_tierc_refusals_that_are_not_consent(sync_settings, auth, tmp_path):
    plate = _plate_png(tmp_path / "plate.png")
    with _client(sync_settings) as c:
        _project(c, auth)
        url = f"/api/v0/projects/{PROJECT}/tierc"
        ok = {"plate_path": str(plate), "shot_id": "hero", "consent_record": "signed 2026-10-01"}
        assert c.post("/api/v0/projects/absent/tierc", json=ok, headers=auth).status_code == 404
        assert c.post("/api/v0/projects/Bad..Id/tierc", json=ok, headers=auth).status_code == 422
        assert c.post(url, json={**ok, "shot_id": "Not A Slug"}, headers=auth).status_code == 422
        assert c.post(url, json={**ok, "shot_id": "../x"}, headers=auth).status_code == 422
        assert c.post(url, json={**ok, "plate_path": "plate.png"}, headers=auth).status_code == 422
        r = c.post(url, json={**ok, "plate_path": str(tmp_path / "absent.png")}, headers=auth)
        assert r.status_code == 422 and "no such file" in r.json()["errors"][0]


def test_tierc_in_process_registers_a_tier_c_packet(sync_settings, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_PIXELS_INPROC", "1")
    plate = _plate_png(tmp_path / "plate.png")
    with _client(sync_settings) as c:
        _project(c, auth, "client-job", nda=True)
        r = c.post(
            "/api/v0/projects/client-job/tierc",
            json={"plate_path": str(plate), "shot_id": "hero", "consent_record": "signed form 2026-10-01"},
            headers=auth,
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["tier"] == "C" and body["stub"] is True and body["status"] == "ingested"
        packet_id = body["packet_id"]
        folder = Path(body["packet_dir"])
        assert folder.parent == sync_settings.packets_dirs[0] / "client-job"
        assert folder.name.startswith("tierc-hero-") and ":" not in folder.name

        rec = c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()
        assert rec["status"] == "ingested" and rec["tier"] == "C" and rec["registered"] is True
        assert rec["project_id"] == "client-job" and rec["shot_id"] == "hero"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["estimated"]["client"]["consent_record"] == "signed form 2026-10-01"
        assert manifest["policy"]["custody"] == {"nda": True, "allowed_grades": ["C0", "C1"]}
        assert (folder / "client_plate.png").is_file() and (folder / "depth_estimated.exr").is_file()
        packets = json.loads(
            (sync_settings.projects_dir / "client-job" / "packets.json").read_text(encoding="utf-8")
        )
        assert packets["packets"][packet_id]["tier"] == "C"


def _fake_worker(
    monkeypatch, token: str, seen: list[dict], *, answer: httpx.Response | None = None, error=None
) -> None:
    """Play the pixels worker's POST /tierc by replacing the one `httpx.post` the daemon makes to it."""
    from plate_daemon.routes import tierc as tierc_routes
    from plate_pixels.backends import Backends
    from plate_pixels.tierc import build_tier_c_packet

    def post(url: str, **kw) -> httpx.Response:
        assert url.startswith("http://127.0.0.1:") and url.endswith("/tierc")
        assert kw["headers"]["Authorization"] == f"Bearer {token}"
        assert kw["trust_env"] is False  # a proxy in the environment never sees the plate path
        body = kw["json"]
        seen.append(body)
        if error is not None:
            raise error
        if answer is not None:
            return answer
        packet = build_tier_c_packet(
            body["plate_path"],
            body["out_dir"],
            Backends(),
            body["project_id"],
            body["shot_id"],
            body["consent_record"],
            nda=body["nda"],
        )
        manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
        return httpx.Response(
            200,
            json={
                "stub": True,
                "packet_dir": str(packet),
                "packet_id": manifest["packet_id"],
                "tier": manifest["tier"],
            },
        )

    monkeypatch.setattr(tierc_routes.httpx, "post", post)


def test_tierc_over_http_calls_the_worker_with_the_daemons_token(sync_settings, auth, tmp_path, monkeypatch):
    monkeypatch.delenv("PLATE_PIXELS_INPROC", raising=False)
    plate = _plate_png(tmp_path / "plate.png")
    seen: list[dict] = []
    with _client(sync_settings) as c:
        _fake_worker(monkeypatch, sync_settings.token, seen)
        _project(c, auth)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/tierc",
            json={"plate_path": str(plate), "shot_id": "wide-1", "consent_record": "email 2026-10-02"},
            headers=auth,
        )
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "ingested" and r.json()["tier"] == "C"
        assert len(seen) == 1 and seen[0]["consent_record"] == "email 2026-10-02"
        assert seen[0]["nda"] is False and seen[0]["project_id"] == PROJECT
        assert Path(seen[0]["out_dir"]).parent == sync_settings.packets_dirs[0] / PROJECT


@pytest.mark.parametrize(
    ("worker_status", "worker_body", "want_status", "want_key"),
    [
        (428, {"error": "consent record required"}, 428, "error"),
        (400, {"error": "cannot read the plate"}, 422, "errors"),
        (401, {"error": "unauthorized"}, 502, "error"),
        (503, {"error": "not enough VRAM"}, 503, "error"),
        (500, {"error": "boom"}, 502, "error"),
    ],
)
def test_tierc_maps_the_workers_failures(
    sync_settings, auth, tmp_path, monkeypatch, worker_status, worker_body, want_status, want_key
):
    monkeypatch.delenv("PLATE_PIXELS_INPROC", raising=False)
    plate = _plate_png(tmp_path / "plate.png")
    with _client(sync_settings) as c:
        _fake_worker(
            monkeypatch, sync_settings.token, [], answer=httpx.Response(worker_status, json=worker_body)
        )
        _project(c, auth)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/tierc",
            json={"plate_path": str(plate), "shot_id": "hero", "consent_record": "ok"},
            headers=auth,
        )
        assert r.status_code == want_status and want_key in r.json(), r.text
        assert not any((sync_settings.packets_dirs[0] / PROJECT).glob("tierc-*"))  # no leftover folder


def test_tierc_against_the_real_pixels_worker_on_a_socket(sync_settings, auth, tmp_path, monkeypatch):
    """The contract between the two processes: the worker's Host guard, its bearer check and its /tierc answer."""
    from plate_pixels.backends import Backends
    from plate_pixels.server import create_app as create_worker

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_worker(sync_settings.token, Backends(), port=port), log_level="warning", lifespan="on"
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive(), "the worker did not start"
        time.sleep(0.02)
    try:
        monkeypatch.delenv("PLATE_PIXELS_INPROC", raising=False)
        monkeypatch.setenv("PLATE_PIXELS_PORT", str(port))
        plate = _plate_png(tmp_path / "plate.png")
        with _client(sync_settings) as c:
            _project(c, auth)
            r = c.post(
                f"/api/v0/projects/{PROJECT}/tierc",
                json={"plate_path": str(plate), "shot_id": "hero", "consent_record": "signed"},
                headers=auth,
            )
            assert r.status_code == 202, r.text
            assert r.json()["stub"] is True and r.json()["status"] == "ingested" and r.json()["tier"] == "C"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


def test_tierc_with_the_worker_down_is_503_not_a_hang(sync_settings, auth, tmp_path, monkeypatch, free_port):
    monkeypatch.delenv("PLATE_PIXELS_INPROC", raising=False)
    monkeypatch.setenv("PLATE_PIXELS_PORT", str(free_port()))  # nothing listens there
    plate = _plate_png(tmp_path / "plate.png")
    with _client(sync_settings) as c:
        _project(c, auth)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/tierc",
            json={"plate_path": str(plate), "shot_id": "hero", "consent_record": "ok"},
            headers=auth,
        )
        assert r.status_code == 503 and r.json()["error"] == "pixels_unavailable", r.text
        assert "PLATE_PIXELS_INPROC" in r.json()["detail"]
        assert not any((sync_settings.packets_dirs[0] / PROJECT).glob("tierc-*"))


# ---------------------------------------------------------------------------- the client packet kit


def _kit_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "kit"
    folder.mkdir()
    for name in ("CAM_a_camera.json", "CAM_a_legend.txt"):
        shutil.copyfile(KIT_FIXTURE / name, folder / name)
    shutil.copyfile(ROOM_A / "beauty.exr", folder / "CAM_a_packet.exr")
    return folder


def _tiff16(path: Path, width: int = 640, height: int = 360) -> Path:
    import tifffile

    y, x = np.mgrid[0:height, 0:width]
    rgb = np.stack([x * 100 // width, y * 100 // height, (x + y) % 100], axis=-1) / 100.0
    tifffile.imwrite(str(path), (rgb * 65535).astype(np.uint16), photometric="rgb")
    return path


@needs_room_a
def test_kit_becomes_a_tier_a_packet(sync_settings, auth, tmp_path):
    kit = _kit_folder(tmp_path)
    with _client(sync_settings) as c:
        _project(c, auth, "client-kit")
        r = c.post(
            "/api/v0/projects/client-kit/kit",
            json={
                "folder": str(kit),
                "camera": "CAM_a",
                "plate_path": str(_tiff16(tmp_path / "plate.tif")),
                "shot_id": "cam-a",
            },
            headers=auth,
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["tier"] == "A" and body["status"] == "ingested" and isinstance(body["flags"], list)
        folder = Path(body["packet_dir"])
        assert folder.parent == sync_settings.packets_dirs[0] / "client-kit"
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["host"]["plugin"]["name"] == "plate_client_kit"
        assert manifest["project_id"] == "client-kit" and manifest["shot_id"] == "cam-a"
        rec = c.get(f"/api/v0/packets/{body['packet_id']}", headers=auth).json()
        assert rec["status"] == "ingested" and rec["tier"] == "A" and rec["registered"] is True
        # The coverage in the legend came from the EXR's Cryptomatte pixels through plate_pixels.
        legend = json.loads((folder / "id_legend.json").read_text(encoding="utf-8"))
        assert any(e.get("pixel_share", 0) > 0 for e in legend["entries"])


@needs_room_a
def test_kit_refusals(sync_settings, auth, tmp_path):
    kit = _kit_folder(tmp_path)
    plate = _tiff16(tmp_path / "plate.tif")
    with _client(sync_settings) as c:
        _project(c, auth, "client-kit")
        url = "/api/v0/projects/client-kit/kit"
        ok = {"folder": str(kit), "camera": "CAM_a", "plate_path": str(plate)}
        assert c.post("/api/v0/projects/absent/kit", json=ok, headers=auth).status_code == 404
        assert c.post(url, json={**ok, "folder": "kit"}, headers=auth).status_code == 422
        assert c.post(url, json={**ok, "plate_path": "plate.tif"}, headers=auth).status_code == 422
        assert c.post(url, json={**ok, "shot_id": "Not A Slug"}, headers=auth).status_code == 422
        r = c.post(url, json={**ok, "camera": "CAM_nope"}, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "kit_invalid"
        wrong_size = _tiff16(tmp_path / "small.tif", 320, 180)
        r = c.post(url, json={**ok, "plate_path": str(wrong_size)}, headers=auth)
        assert r.status_code == 422 and "never resampled" in r.json()["errors"][0]
        assert not (sync_settings.packets_dirs[0] / "client-kit").exists() or not any(
            (sync_settings.packets_dirs[0] / "client-kit").iterdir()
        )


# ---------------------------------------------------------------------------- scrub, verdict, accept


@needs_room_a
def test_scrub_then_verdict_then_accept(sync_settings, auth, labels_file):
    with _client(sync_settings, EchoEngine()) as c:
        run_id = _done_run(c, auth)
        v = f"/api/v0/runs/{run_id}/verdict"

        # A verdict or an accept without a logged full scrub is refused.
        for sid in ("scrub-nope", ""):
            r = c.post(
                v,
                json={"verdict": "ship", "reasons": [], "scrub_log_id": sid or "x", "rater": "t"},
                headers=auth,
            )
            assert r.status_code == 409 and r.json()["code"] == "SCRUB_REQUIRED"
        r = c.post(f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": "scrub-nope"}, headers=auth)
        assert r.status_code == 409 and r.json()["code"] == "SCRUB_REQUIRED"

        # A partial scrub is logged, but it is not a full scrub.
        partial = _scrub(c, auth, run_id, quadrants=3)
        assert partial.status_code == 200 and partial.json()["full_scrub"] is False
        assert _scrub(c, auth, run_id, quadrants=4, zoom=False).json()["full_scrub"] is False
        pid = partial.json()["scrub_log_id"]
        r = c.post(
            v, json={"verdict": "ship", "reasons": [], "scrub_log_id": pid, "rater": "t"}, headers=auth
        )
        assert r.status_code == 409 and r.json()["code"] == "SCRUB_REQUIRED"
        assert not labels_file.exists()

        full = _scrub(c, auth, run_id)
        assert full.status_code == 200 and full.json()["full_scrub"] is True
        sid = full.json()["scrub_log_id"]
        assert sid.startswith("scrub-") and sid != pid
        assert _scrub(c, auth, run_id, quadrants=5).status_code == 422
        assert _scrub(c, auth, "r-absent").status_code == 404

        body = {"verdict": "fix", "reasons": ["P1", "A2"], "scrub_log_id": sid, "rater": "aasish"}
        assert c.post(v, json={**body, "verdict": "maybe"}, headers=auth).status_code == 422
        assert c.post(v, json={**body, "reasons": ["ZZ9"]}, headers=auth).status_code == 422
        assert (
            c.post(v, json={**body, "verdict": "ship"}, headers=auth).status_code == 422
        )  # a Ship has no codes
        assert c.post(v, json={**body, "rater": ""}, headers=auth).status_code == 422
        assert c.post(v, json={**body, "seconds": -1}, headers=auth).status_code == 422
        assert not labels_file.exists()

        r = c.post(v, json=body, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["decision"] == "rerolled" and r.json()["reasons"] == ["P1", "A2"]
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        lines = [json.loads(x) for x in labels_file.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        label = lines[0]
        assert (label["v"], label["rater"], label["verdict"], label["reasons"]) == (
            1,
            "aasish",
            "fix",
            ["P1", "A2"],
        )
        assert label["run_id"] == label["take_id"] == run_id and label["candidate"] == run["outputs"][0]
        assert label.get("gate_report") == run.get("gate_report_path")
        assert label["at"].endswith("Z") and "seconds" not in label

        # A relabel of the same take is a later line; the calibration report keeps the last.
        r = c.post(
            v,
            json={"verdict": "ship", "reasons": [], "scrub_log_id": sid, "rater": "aasish", "seconds": 12.34},
            headers=auth,
        )
        assert r.status_code == 200 and r.json()["decision"] == "accepted"
        from plate_gates.calibration import dedupe_latest, load_labels

        loaded = load_labels(labels_file)
        assert [x["verdict"] for x in loaded] == ["fix", "ship"] and loaded[1]["seconds"] == 12.3
        assert [x["verdict"] for x in dedupe_latest(loaded)] == ["ship"]

        # The ledger holds the scrubs and the verdicts, the chain verifies, and the fold sees the decision.
        led = Ledger(sync_settings.projects_dir / PROJECT)
        assert led.verify() == (True, None)
        mine = [r for r in led.records() if r.get("run_id") == run_id]
        assert [r["kind"] for r in mine if r["kind"] in ("scrub", "verdict")] == [
            "scrub",
            "scrub",
            "scrub",
            "verdict",
            "verdict",
        ]
        verdict_rec = [r for r in mine if r["kind"] == "verdict"][0]
        assert verdict_rec["by"] == "aasish" and verdict_rec["reason"] == "P1, A2"
        assert verdict_rec["data"]["scrub_log_id"] == sid and verdict_rec["engine_id"] == "fake"
        assert led.runs()[run_id].decision == "accepted"

        # Accept works against the same full scrub, and not against a partial one.
        r = c.post(f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": pid}, headers=auth)
        assert r.status_code == 409
        r = c.post(f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": sid}, headers=auth)
        assert r.status_code == 200 and r.json()["accepted"]["scrub_log_id"] == sid


@needs_room_a
def test_a_take_that_is_not_done_cannot_be_scrubbed_or_judged(sync_settings, auth, labels_file):
    with _client(sync_settings, EchoEngine()) as c:
        _project(c, auth)
        manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        r = c.post(
            "/api/v0/packets",
            json={"packet_path": str(ROOM_A), "sender": {"name": "t", "version": "0"}},
            headers=hdr,
        )
        graph = {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": r.json()["packet_id"], "tier": "A"},
                {"id": "e", "kind": "Edit", "params": {"engine": "fake"}},
            ],
            "edges": [{"id": "p->e", "source": "p", "target": "e"}],
        }
        assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200
        run_id = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth).json()["run"][
            "id"
        ]
        r = _scrub(c, auth, run_id)
        assert r.status_code == 409 and r.json()["error"] == "not_judgeable" and r.json()["state"] == "QUOTED"
        r = c.post(
            f"/api/v0/runs/{run_id}/verdict",
            json={"verdict": "ship", "reasons": [], "scrub_log_id": "scrub-x"},
            headers=auth,
        )
        assert r.status_code == 409 and r.json()["error"] == "not_judgeable"
        assert not labels_file.exists()


def test_label_lines_and_reason_codes(tmp_path, monkeypatch):
    out = tmp_path / "deep" / "labels.jsonl"
    at = datetime(2026, 10, 17, 9, 41, 7, 123456, tzinfo=UTC)
    rec = verdicts.build_label(
        at=at, rater=" aasish ", run_id="r1", verdict="render_it", reasons=["P2"], candidate="ab" * 32
    )
    assert list(rec) == ["v", "at", "rater", "take_id", "plate", "candidate", "run_id", "verdict", "reasons"]
    assert rec["at"] == "2026-10-17T09:41:07Z" and rec["rater"] == "aasish" and rec["plate"] is None
    # a crash that left a torn last line must not glue the next record onto it
    out.parent.mkdir()
    out.write_bytes(b'{"v":1,"torn')
    verdicts.append_label(out, rec)
    raw = out.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    assert json.loads(raw.splitlines()[-1]) == rec
    with pytest.raises(verdicts.VerdictError):
        verdicts.build_label(at=at, rater="", run_id="r1", verdict="fix", reasons=[], candidate="c")
    assert verdicts.check_reasons("fix", []) == []  # reasons are optional on Fix and Render-it
    assert verdicts.check_reasons("fix", ["P1", "P1", "A2"]) == ["P1", "A2"]
    with pytest.raises(verdicts.VerdictError):
        verdicts.check_reasons("ship", ["P1"])
    with pytest.raises(verdicts.VerdictError):
        verdicts.check_reasons("fix", "P1")
    # the labels path: settings first, then PLATE_LABELS, then the repo's calib folder
    monkeypatch.delenv("PLATE_LABELS", raising=False)
    assert verdicts.labels_path() == ROOT / "calib" / "labels.jsonl"
    monkeypatch.setenv("PLATE_LABELS", str(out))
    assert verdicts.labels_path() == out

    class WithPath:
        labels_path = tmp_path / "x.jsonl"

    assert verdicts.labels_path(WithPath()) == tmp_path / "x.jsonl"


# ---------------------------------------------------------------------------- the timer


def _local_noon() -> datetime:
    return datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0).astimezone(UTC)


def _timer_client(settings: Settings, clock: dict) -> TestClient:
    c = TestClient(create_app(settings), base_url=BASE)
    c.app.state.clock = lambda: clock["now"]
    return c


def test_timer_start_stop_and_today(sync_settings, auth):
    clock = {"now": _local_noon()}
    with _timer_client(sync_settings, clock) as c:
        post = lambda body: c.post("/api/v0/timer", json=body, headers=auth)  # noqa: E731
        assert c.get("/api/v0/timer/today", headers=auth).json()["rows"] == []

        r = post({"deliverable_id": "hero-still", "action": "start"})
        assert r.status_code == 200 and r.json()["already_running"] is False
        assert post({"deliverable_id": "hero-still", "action": "start"}).json()["already_running"] is True
        r = post({"deliverable_id": "walkthrough-02", "action": "start"})
        assert r.status_code == 409 and r.json()["error"] == "timer_running"
        assert "another deliverable is running" in r.json()["detail"]
        assert post({"deliverable_id": "walkthrough-02", "action": "stop"}).status_code == 409
        running = c.get("/api/v0/timer/today", headers=auth).json()["running"]
        assert running["deliverable_id"] == "hero-still"

        clock["now"] += timedelta(seconds=90)
        r = post({"deliverable_id": "hero-still", "action": "stop"})
        assert r.status_code == 200 and r.json()["minutes"] == 1.5
        assert post({"deliverable_id": "hero-still", "action": "stop"}).json()["error"] == "timer_not_running"

        # the minutes are in the (workspace) ledger as human_minutes records
        led = Ledger(sync_settings.home / timer.WORKSPACE_DIR)
        recs = [r for r in led.records() if r["kind"] == "human_minutes"]
        assert len(recs) == 1 and recs[0]["data"]["deliverable_id"] == "hero-still"
        assert recs[0]["data"]["minutes"] == 1.5 and recs[0]["data"]["project_id"] is None

        post({"deliverable_id": "hero-still", "action": "start"})
        clock["now"] += timedelta(seconds=30)
        post({"deliverable_id": "hero-still", "action": "stop"})
        post({"deliverable_id": "walkthrough-02", "action": "start"})
        clock["now"] += timedelta(minutes=41)
        post({"deliverable_id": "walkthrough-02", "action": "stop"})
        today = c.get("/api/v0/timer/today", headers=auth).json()
        assert today["rows"] == [
            {"deliverable_id": "hero-still", "minutes": 2.0},
            {"deliverable_id": "walkthrough-02", "minutes": 41.0},
        ]
        assert today["totals"] == {"minutes": 43.0} and today["running"] is None
        assert led.verify() == (True, None)

        # yesterday's minutes are not today's
        clock["now"] -= timedelta(days=1, minutes=-30)
        post({"deliverable_id": "old-job", "action": "start"})
        clock["now"] += timedelta(minutes=10)
        post({"deliverable_id": "old-job", "action": "stop"})
        clock["now"] += timedelta(days=1)
        assert "old-job" not in [
            r["deliverable_id"] for r in c.get("/api/v0/timer/today", headers=auth).json()["rows"]
        ]


def test_timer_validation(sync_settings, auth):
    clock = {"now": _local_noon()}
    with _timer_client(sync_settings, clock) as c:
        post = lambda body: c.post("/api/v0/timer", json=body, headers=auth)  # noqa: E731
        assert post({"deliverable_id": "  ", "action": "start"}).status_code == 422
        assert post({"deliverable_id": "a\nb", "action": "start"}).status_code == 422
        assert post({"deliverable_id": "x" * 121, "action": "start"}).status_code == 422
        assert post({"deliverable_id": "x", "action": "pause"}).status_code == 422
        assert post({"deliverable_id": "x", "action": "start", "project_id": "absent"}).status_code == 404
        assert c.get("/api/v0/timer/today?project_id=absent", headers=auth).status_code == 404
        assert c.post("/api/v0/timer", json={"deliverable_id": "x", "action": "start"}).status_code == 401


def test_timer_survives_a_restart_and_can_belong_to_a_project(sync_settings, auth):
    clock = {"now": _local_noon()}
    with _timer_client(sync_settings, clock) as c:
        _project(c, auth, "job-1")
        r = c.post(
            "/api/v0/timer",
            json={"deliverable_id": "hero", "action": "start", "project_id": "job-1"},
            headers=auth,
        )
        assert r.status_code == 200 and r.json()["project_id"] == "job-1"
    clock["now"] += timedelta(minutes=5)
    with _timer_client(sync_settings, clock) as c:  # a new app, the same home: the timer is still running
        today = c.get("/api/v0/timer/today", headers=auth).json()
        assert today["running"]["deliverable_id"] == "hero"
        r = c.post("/api/v0/timer", json={"deliverable_id": "hero", "action": "stop"}, headers=auth)
        assert r.status_code == 200 and r.json()["minutes"] == 5.0 and r.json()["project_id"] == "job-1"
        led = Ledger(sync_settings.projects_dir / "job-1")
        assert [r["data"]["minutes"] for r in led.records() if r["kind"] == "human_minutes"] == [5.0]
        assert not (sync_settings.home / timer.WORKSPACE_DIR / "runs.jsonl").exists()
        everything = c.get("/api/v0/timer/today", headers=auth).json()
        scoped = c.get("/api/v0/timer/today?project_id=job-1", headers=auth).json()
        assert everything["rows"] == scoped["rows"] == [{"deliverable_id": "hero", "minutes": 5.0}]
        assert not (sync_settings.home / timer.WORKSPACE_DIR / RUNNING).exists()


RUNNING = timer.RUNNING_JSON


# ---------------------------------------------------------------------------- drop to resolve


def _structured_png(path: Path) -> Path:
    rng = np.random.default_rng(7)
    base = cv2.GaussianBlur(rng.random((192, 256, 3)).astype(np.float32), (0, 0), 6)
    base = (base - base.min()) / (base.max() - base.min())
    assert cv2.imwrite(str(path), (base * 255).astype(np.uint8))
    return path


def test_resolve_by_content_hash_then_phash_then_nothing(sync_settings, auth, tmp_path):
    original = _structured_png(tmp_path / "original.png")
    with _client(sync_settings) as c:
        _project(c, auth, "job-1")
        cas = c.app.state.cas_for("job-1")
        key = cas.put_bytes(original.read_bytes(), "png")
        PhashIndex(c.app.state.index, "job-1").add_file(original, key)

        copy = tmp_path / "elsewhere" / "copy.png"
        copy.parent.mkdir()
        copy.write_bytes(original.read_bytes())
        r = c.post("/api/v0/assets/resolve", json={"path": str(copy)}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["by"] == "content_hash" and r.json()["content_hash"] == key
        assert r.json()["project_id"] == "job-1"

        jpeg = tmp_path / "screenshot.jpg"
        assert cv2.imwrite(str(jpeg), cv2.imread(str(original)), [cv2.IMWRITE_JPEG_QUALITY, 85])
        r = c.post("/api/v0/assets/resolve", json={"path": str(jpeg)}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["by"] == "phash" and r.json()["content_hash"] == key
        assert r.json()["distance"] <= 12 and r.json()["project_id"] == "job-1"
        scoped = c.post(
            "/api/v0/assets/resolve", json={"path": str(jpeg), "project_id": "job-1"}, headers=auth
        )
        assert scoped.json() == r.json()

        other = tmp_path / "other.png"
        rng = np.random.default_rng(99)
        assert cv2.imwrite(str(other), (rng.random((192, 256, 3)) * 255).astype(np.uint8))
        r = c.post("/api/v0/assets/resolve", json={"path": str(other)}, headers=auth)
        assert r.status_code == 404 and r.json()["error"] == "no_match"

        assert (
            c.post("/api/v0/assets/resolve", json={"path": "relative.png"}, headers=auth).status_code == 422
        )
        r = c.post("/api/v0/assets/resolve", json={"path": str(tmp_path / "absent.png")}, headers=auth)
        assert r.status_code == 404
        r = c.post(
            "/api/v0/assets/resolve", json={"path": str(original), "project_id": "absent"}, headers=auth
        )
        assert r.status_code == 404


@needs_room_a
def test_resolve_by_lineage_and_a_foreign_lineage(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine()) as c:
        run_id = _done_run(c, auth)
        dest = tmp_path / "take.png"
        r = c.post(f"/api/v0/runs/{run_id}/export", json={"dest": str(dest)}, headers=auth)
        assert r.status_code == 200, r.text
        r = c.post("/api/v0/assets/resolve", json={"path": str(dest)}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["by"] == "lineage" and r.json()["run_id"] == run_id and r.json()["known_run"] is True
        assert r.json()["project_id"] == PROJECT

        # the same pixels claiming a run this daemon never made: the claim is reported, the run is not trusted
        foreign = tmp_path / "foreign.png"
        foreign.write_bytes(dest.read_bytes())
        lineage.embed(
            foreign,
            lineage.Lineage(
                project_id="other-studio",
                node_id="n",
                run_id="r-from-elsewhere",
                packet_id="p",
                engine_id="e",
            ),
        )
        r = c.post("/api/v0/assets/resolve", json={"path": str(foreign)}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["by"] == "lineage" and r.json()["run_id"] is None and r.json()["known_run"] is False


# ---------------------------------------------------------------------------- the outbound log


def test_outbound_tail(sync_settings, auth):
    with _client(sync_settings) as c:
        _project(c, auth, "job-1")
        url = "/api/v0/projects/job-1/outbound"
        assert c.get(url, headers=auth).json() == {"entries": []}
        log = OutboundLog.for_project(sync_settings.projects_dir / "job-1")
        for i in range(3):
            eid = f"01J{i}"
            log.request(
                entry_id=eid,
                run_id=f"r{i}",
                route_id="gemini-direct",
                host="generativelanguage.googleapis.com",
                method="post",
                path="/v1/models/x:generate",
                bytes_out=1000 + i,
                custody_grade="C2",
                opaque_ids=["aaaa1111bbbb2222"],
                body_hash="f" * 64,
            )
            if i < 2:
                log.response(eid, status=200, bytes_in=2048)
            else:
                log.error(eid, error="timeout")
        r = c.get(url, headers=auth)
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert [e["id"] for e in entries] == ["01J0", "01J1", "01J2"]
        assert (
            entries[0]["status"] == 200 and entries[0]["bytes_in"] == 2048 and entries[0]["method"] == "POST"
        )
        assert entries[2]["error"] == "timeout" and entries[2]["status"] is None
        assert [e["id"] for e in c.get(f"{url}?tail=2", headers=auth).json()["entries"]] == ["01J1", "01J2"]
        assert c.get(f"{url}?tail=0", headers=auth).status_code == 422
        assert c.get(f"{url}?tail=1001", headers=auth).status_code == 422
        assert c.get("/api/v0/projects/absent/outbound", headers=auth).status_code == 404
        assert c.get(url).status_code == 401
