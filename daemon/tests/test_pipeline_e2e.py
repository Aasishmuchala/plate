"""The pipeline end to end on the committed Blender packet `fixtures/packets/room_a` (router plan T25).

POST packet -> graph -> quote a node -> approve (auto, under the cap) -> run -> DONE with a gate report
whose registration residual is under 1 px, whose outside-mask pixels are the plate bit for bit and which
flags no invented architecture. Then the refusals: a FakeEngine that rejects the submit ends FAILED with
the reservation refunded, a refusing one REFUSED, an expired quote answers 409, and a D route on an NDA
project answers 403 with `CustodyViolation` before anything is built. `EchoEngine` is a FakeEngine whose
result is the PNG egress sent it (optionally shifted), so the gates score real pixels; the plain
FakeEngine's noise would only ever fail them.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import gating, ledger, lineage
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.pipeline import decode_png16
from plate_daemon.sse import EventFilter
from plate_daemon.timeutil import utcnow

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)

PIPELINE_STATES = [
    "QUOTED",
    "APPROVED",
    "QUEUED",
    "STAGING",
    "SUBMITTED",
    "RUNNING",
    "FETCHING",
    "NORMALIZING",
    "GATING",
]


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, moved `shift_px` to the right."""

    def __init__(self, *args, shift_px: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.shift_px = shift_px
        self.sent: dict[str, bytes] = {}
        self.payloads: list[dict] = []

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.payloads.append(payload)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)  # call log, expiry
        data = self.sent[result_url[len(RESULT_SCHEME) :]]
        if self.shift_px:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
            img = np.roll(img, self.shift_px, axis=1)
            data = bytes(cv2.imencode(".png", img)[1])
        return data


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


@pytest.fixture
def dev_settings(sync_settings, monkeypatch) -> Settings:
    """The seed's dev driver (`POST /runs/{id}/queue`, `/step`) is mounted only under PLATE_DEV=1 (decision 36)."""
    monkeypatch.setenv("PLATE_DEV", "1")
    return Settings.load()


def _client(settings: Settings, engine) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict, *, nda: bool = False, engine_param: str = "fake") -> str:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": engine_param}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _quote(c: TestClient, auth: dict, **body) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _plate16() -> np.ndarray:
    from plate_pixels import imageio

    return np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)


# ---------------------------------------------------------------------------- the happy path


def test_packet_to_gated_output_on_the_fake_engine(sync_settings, auth, tmp_path, monkeypatch):
    engine = EchoEngine()
    with _client(sync_settings, engine) as c:
        packet_id = _setup(c, auth)
        q = _quote(c, auth)
        run_id = q["run"]["id"]
        assert q["run"]["state"] == "QUOTED" and q["run"]["node_id"] == "e"
        assert q["quote"]["custody_grade"] == "C0" and q["quote"]["price_usd"] < q["auto_cap_usd"]

        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
        assert r.status_code == 200, r.text
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] == "DONE", run.get("error")
        assert run["approved_by"] == "auto" and run["exportable"] is True
        assert engine.submit_count == 1 and run["provider_job_id"]

        rep = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth)
        assert rep.status_code == 200, rep.text
        rep = rep.json()
        metrics = rep["metrics"]
        assert rep["verdict"] in ("pass", "advisory") and rep["n"] == 1 and rep["tier"] == "A"
        assert metrics["registration"]["residual_px"] <= 1.0
        assert metrics["outside_mask"]["identical_outside"] is True
        assert metrics["invention_flags"] == 0
        status = {ch["metric_id"]: ch["status"] for ch in rep["checks"]}
        for hard in (
            "registration.residual_px",
            "registration.shift_px",
            "outside_mask.de00",
            "gbuffer_edge_recall_r2",
            "invention_flags",
            "material.de00_median",
            "material.de00_p90",
        ):
            assert status[hard] == "pass", (hard, status)
        assert status["feather_band.de00_p99"] == "advisory"  # decision 35: advisory until calibration (T23)
        assert rep["threshold_set_id"] and rep["metric_code_hash"]
        project_dir = sync_settings.projects_dir / PROJECT
        assert (project_dir / rep["path"]).is_file()

        # The output: 16-bit, the plate's size, the plate itself outside the editable mask.
        out_key = run["outputs"][0]
        assert out_key != run["result_key"]
        cas = CAS(project_dir / "assets")
        out = decode_png16(cas.read_bytes(out_key))
        plate = _plate16()
        mask = cv2.imdecode(np.frombuffer(cas.read_bytes(run["normalized"]["mask_key"]), np.uint8), 0) > 127
        assert out.shape == plate.shape == (360, 640, 3) and out.dtype == np.uint16
        assert np.array_equal(out[~mask], plate[~mask])
        assert 0.05 < mask.mean() < 0.5
        assert run["normalized"]["sizemap"]["src"] == [640, 360]

        # The vendor saw a PNG under an opaque name, with no project, shot or camera name anywhere.
        sent = engine.payloads[0]
        text = json.dumps({k: v for k, v in sent.items() if k != "inputs"}) + json.dumps(
            {k: v for k, v in sent["inputs"]["image"].items() if k != "b64"}
        )
        for word in (PROJECT, "room-a", "room_a", "CAM_a", packet_id):
            assert word not in text
        png = base64.b64decode(sent["inputs"]["image"]["b64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and b"tEXt" not in png and b"eXIf" not in png

        # Every move is on the bus, in order.
        recs = c.app.state.bus.read(EventFilter(PROJECT, run_id), 0)
        moves = [r["data"]["to"] for r in recs if r["kind"] == "run.state"]
        assert moves == [*PIPELINE_STATES, "DONE"]

        # The ledger chain holds and carries the run's story.
        led = Ledger(project_dir)
        assert led.verify() == (True, None)
        kinds = [r["kind"] for r in led.records() if r.get("run_id") == run_id]
        assert kinds == ["quoted", "approved", "submitted", "fetched", "done"]

        # The packet is registered by reference; its pixels never entered the project folder.
        packets = json.loads((project_dir / "packets.json").read_text(encoding="utf-8"))["packets"]
        assert packets[packet_id]["path"] == str(ROOM_A)
        assert not any(p.name in ("display.tif", "beauty.exr") for p in project_dir.rglob("*"))

        # Assets in the three forms the UI reads.
        thumb = c.get(f"/api/v0/assets/{out_key}?fmt=thumb", headers=auth)
        assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/png"
        assert cv2.imdecode(np.frombuffer(thumb.content, np.uint8), 1).shape[1] == 320
        half = c.get(f"/api/v0/assets/{out_key}?fmt=rgba16f", headers=auth)
        assert half.status_code == 200 and len(half.content) == 640 * 360 * 8
        assert (half.headers["x-plate-width"], half.headers["x-plate-height"]) == ("640", "360")
        rgba = np.frombuffer(half.content, "<f2").reshape(360, 640, 4)
        assert np.allclose(rgba[..., :3], out.astype(np.float32) / 65535.0, atol=1e-3)
        assert c.get(f"/api/v0/assets/{out_key}?fmt=png", headers=auth).content == cas.read_bytes(out_key)
        assert c.get(f"/api/v0/assets/{'0' * 64}", headers=auth).status_code == 404

        # Export writes the gated composite (with its lineage), never the engine's raw bytes. It goes through
        # `export.export` (decision 39), which adds the lineage and an sRGB ICC profile: the pixels are the
        # composite's, the file bytes are not the CAS object's.
        dest = tmp_path / "out" / "take.png"
        r = c.post(f"/api/v0/runs/{run_id}/export", json={"dest": str(dest)}, headers=auth)
        assert r.status_code == 200, r.text
        assert np.array_equal(decode_png16(dest.read_bytes()), out)
        lin = lineage.read(dest)
        assert lin is not None and lin.run_id == run_id and lin.packet_id == packet_id and lin.node_id == "e"

        # Accepting the take needs a logged full scrub of this run.
        r = c.post(f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": "scrub_1"}, headers=auth)
        assert r.status_code == 409 and r.json()["code"] == "SCRUB_REQUIRED"
        monkeypatch.setattr(
            ledger, "KINDS", ledger.KINDS | {"scrub"}
        )  # T38 adds the kind; written here by hand
        led.append(
            kind="scrub", run_id=run_id, opaque_id="scrub_1", data={"quadrants_visited": 4, "zoom_100": True}
        )
        r = c.post(f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": "scrub_1"}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["accepted"]["scrub_log_id"] == "scrub_1"


# ---------------------------------------------------------------------------- refusals


def test_fail_submit_ends_failed_with_the_reservation_refunded(sync_settings, auth):
    with _client(sync_settings, FakeEngine(behaviour="fail_submit")) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
        assert r.status_code == 200, r.text
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] == "FAILED" and run["reason_code"] == "submit_error"
        assert run["provider_job_id"] is None and run["reserved_usd"] == 0 and run["cost_usd_billed"] is None
        kinds = [e["kind"] for e in c.get(f"/api/v0/runs/{run_id}/events", headers=auth).json()]
        assert "spend.reserve" in kinds and "spend.refund" in kinds and "spend.settle" not in kinds
        failed = [r for r in Ledger(sync_settings.projects_dir / PROJECT).records() if r["kind"] == "failed"]
        assert failed and failed[-1]["run_id"] == run_id and failed[-1]["data"]["refunded"] is True


def test_a_refusing_engine_ends_refused(sync_settings, auth):
    with _client(sync_settings, FakeEngine(behaviour="refuse")) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        assert c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth).status_code == 200
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] == "REFUSED" and run["reason_code"] == "refused"
        assert run["provider_job_id"] and run["outputs"] == []
        assert c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).status_code == 404


def test_an_expired_quote_cannot_be_approved(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        later = utcnow() + timedelta(minutes=16)
        c.app.state.clock = lambda: later
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
        assert r.status_code == 409 and r.json()["error"] == "quote_expired"
        assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"] == "EXPIRED"


def test_a_d_route_on_an_nda_project_is_refused_before_anything_is_built(sync_settings, auth):
    engine = EchoEngine()
    with _client(sync_settings, engine) as c:
        _setup(c, auth, nda=True)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={"engine_id": "nano-banana-2.1"}, headers=auth
        )
        assert r.status_code == 403, r.text
        body = r.json()
        assert body["error"] == "custody" and body["exception"] == "CustodyViolation"
        assert body["grade"] == "D" and body["allowed"] == ["C0", "C1"]
        with c.app.state.index.project(PROJECT) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert engine.submit_count == 0
        assert not (sync_settings.projects_dir / PROJECT / "outbound.jsonl").exists()
        # The local engine is C0, which an NDA project allows.
        assert _quote(c, auth, engine_id="fake")["quote"]["custody_grade"] == "C0"


# ---------------------------------------------------------------------------- gate failure, override, best-of-N, the step driver


def test_a_drifted_result_fails_its_gates_and_exports_only_after_an_override(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine(shift_px=12)) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] == "GATE_FAIL" and run["exportable"] is False
        rep = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()
        failed = {ch["metric_id"] for ch in rep["checks"] if ch["status"] == "fail"}
        assert rep["verdict"] == "fail" and "registration.shift_px" in failed
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True  # the plate is safe even so

        dest = tmp_path / "drift.png"
        assert (
            c.post(f"/api/v0/runs/{run_id}/export", json={"dest": str(dest)}, headers=auth).status_code == 409
        )
        short = c.post(f"/api/v0/runs/{run_id}/override", json={"reason": "ok", "by": "human"}, headers=auth)
        assert short.status_code == 422
        r = c.post(
            f"/api/v0/runs/{run_id}/override",
            json={"reason": "client approved the shifted crop", "by": "human"},
            headers=auth,
        )
        assert r.status_code == 200 and r.json()["exportable"] is True
        assert r.json()["override"]["reason"] == "client approved the shifted crop"
        assert (
            c.post(f"/api/v0/runs/{run_id}/export", json={"dest": str(dest)}, headers=auth).status_code == 200
        )
        overrides = [
            r for r in Ledger(sync_settings.projects_dir / PROJECT).records() if r["kind"] == "override"
        ]
        assert overrides and overrides[-1]["reason"] == "client approved the shifted crop"


def test_best_of_three_runs_under_one_approval_and_is_ranked(sync_settings, auth):
    engine = EchoEngine()
    with _client(sync_settings, engine) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        sibs = c.post(f"/api/v0/runs/{run_id}/fanout", json={"n": 3}, headers=auth)
        assert sibs.status_code == 200, sibs.text
        group = {s["sibling_group"] for s in sibs.json()}
        assert len(sibs.json()) == 3 and len(group) == 1 and None not in group
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
        assert r.status_code == 200 and len(r.json()["runs"]) == 3
        states = [c.get(f"/api/v0/runs/{s['id']}", headers=auth).json()["state"] for s in sibs.json()]
        assert states == ["DONE", "DONE", "DONE"] and engine.submit_count == 3
        ranked = [x for x in c.app.state.bus.read(EventFilter(PROJECT), 0) if x["kind"] == "siblings.ranked"]
        assert len(ranked) == 1 and sorted(ranked[0]["data"]["order"]) == sorted(s["id"] for s in sibs.json())


def test_the_step_driver_runs_pipeline_runs_through_the_real_gates(dev_settings, auth):
    with _client(dev_settings, EchoEngine()) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human", "start": False}, headers=auth)
        assert r.status_code == 200 and r.json()["state"] == "APPROVED"
        assert c.post(f"/api/v0/runs/{run_id}/queue", headers=auth).json()["state"] == "QUEUED"
        state = "QUEUED"
        for _ in range(20):
            state = c.post(f"/api/v0/runs/{run_id}/step", headers=auth).json()["state"]
            if state in ("DONE", "GATE_FAIL", "FAILED"):
                break
        assert state == "DONE"
        report = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["gate_report"]
        assert report.get("stub") is None and report["metrics"]["invention_flags"] == 0


def test_a_region_crop_sends_only_the_mask_box_and_lands_back_in_place(sync_settings, auth):
    engine = EchoEngine()
    with _client(sync_settings, engine) as c:
        _setup(c, auth)
        q = _quote(c, auth, region_crop=True)
        assert q["run"]["inputs"]["staging"]["region_crop"] is True
        c.post(f"/api/v0/runs/{q['run']['id']}/approve", json={"by": "auto"}, headers=auth)
        run = c.get(f"/api/v0/runs/{q['run']['id']}", headers=auth).json()
        assert run["state"] == "DONE", run.get("error")
        x0, y0, x1, y1 = run["normalized"]["sizemap"]["region"]
        sent = cv2.imdecode(np.frombuffer(next(iter(engine.sent.values())), np.uint8), cv2.IMREAD_UNCHANGED)
        assert sent.shape[:2] == (y1 - y0, x1 - x0) and (x1 - x0, y1 - y0) != (640, 360)
        rep = c.get(f"/api/v0/runs/{run['id']}/gate", headers=auth).json()
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True
        assert rep["metrics"]["registration"]["residual_px"] <= 1.0


def test_a_restarted_daemon_drives_on_the_runs_it_left_in_flight(dev_settings, auth):
    with _client(dev_settings, EchoEngine()) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human", "start": False}, headers=auth)
        assert c.post(f"/api/v0/runs/{run_id}/queue", headers=auth).json()["state"] == "QUEUED"
    engine = EchoEngine()
    with _client(dev_settings, engine) as c:  # the new daemon's start-up resumes the queued run
        deadline = time.monotonic() + 60
        state = "QUEUED"
        while state not in ("DONE", "GATE_FAIL", "FAILED") and time.monotonic() < deadline:
            time.sleep(0.05)
            state = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["state"]
        assert state == "DONE" and engine.submit_count == 1


def test_a_mask_node_narrows_the_edit_and_a_locked_id_is_refused(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        packet_id = _setup(c, auth)
        graph = {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A"},
                {"id": "m", "kind": "Mask", "params": {"ids": ["o:929c9e95"]}},
                {"id": "e", "kind": "Edit", "params": {"engine": "fake"}},
            ],
            "edges": [
                {"id": "p->m", "source": "p", "target": "m"},
                {"id": "m->e", "source": "m", "target": "e"},
            ],
        }
        assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200
        q = _quote(c, auth)
        assert q["run"]["inputs"]["mask"] == {"kind": "ids", "ids": ["o:929c9e95"], "source": "mask:m"}
        c.post(f"/api/v0/runs/{q['run']['id']}/approve", json={"by": "auto"}, headers=auth)
        run = c.get(f"/api/v0/runs/{q['run']['id']}", headers=auth).json()
        assert run["state"] == "DONE"
        share = run["normalized"]["composite"]["mask_share"]
        assert 0.02 < share < 0.08  # the sky alone (legend pixel_share 0.0525)

        graph["nodes"][1]["params"]["ids"] = ["o:57b3d007"]  # ARCH_wall_N is locked by the packet policy
        assert c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth).status_code == 200
        r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "mask_locked"


def test_a_plate_engines_adapter_runs_behind_the_bridge(sync_settings, auth, monkeypatch):
    """The local Qwen adapter (C0, so an NDA project may use it) in its deterministic stub mode: a real
    `plate_engines` adapter driven through `AdapterBridge`. Its blurred-plus-noise output drifts from the
    plate inside the feather band, and the gates say so."""
    monkeypatch.setenv("PLATE_STUB_LOCAL", "1")
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth, nda=True)
        q = _quote(c, auth, engine_id="qwen-image-edit-2509", prompt="late blue hour")
        run_id = q["run"]["id"]
        assert q["quote"]["custody_grade"] == "C0" and q["run"]["route"]["provider"] == "local"
        assert c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth).status_code == 200
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] in ("DONE", "GATE_FAIL"), run.get("error")
        assert run["provider_job_id"].startswith("qwen-image-edit-2509")
        rep = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()
        assert rep["engine"]["id"] == "qwen-image-edit-2509"
        assert rep["metrics"]["outside_mask"]["identical_outside"] is True
        assert (sync_settings.home / "cache" / "adapters" / run_id).is_dir()
        # A cloud engine that declares no sizes is refused at the quote rather than guessed at.
        r = c.post(
            f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={"engine_id": "flux-3-image"}, headers=auth
        )
        assert r.status_code == 403  # flux is D: custody speaks first on an NDA project


# ---------------------------------------------------------------------------- the gate wiring on its own


def test_run_gates_without_a_packet_scores_identity_and_a_leak():
    rng = np.random.default_rng(1)
    plate = (rng.integers(0, 65535, (96, 128, 3))).astype(np.uint16)
    mask = np.zeros((96, 128), dtype=bool)
    mask[20:60, 30:90] = True
    out = plate.copy()
    out[mask] = np.clip(out[mask].astype(np.int64) + 300, 0, 65535).astype(np.uint16)
    reg = {"shift_px": 0.0, "residual_px": 0.1, "registered": True, "camera_drift": False}
    rep = gating.run_gates(plate, out, None, None, "C", {"id": "fake"}, mask=mask, registration=reg)
    checks = {c.metric_id: c.status for c in rep.checks}
    assert checks["outside_mask.de00"] == "pass" and "gbuffer_edge_recall_r2" not in checks
    assert rep.metrics["outside_mask"]["identical_outside"] is True
    assert any("tier C" in n for n in rep.backend_notes) and rep.metrics["depth_agreement"] is None

    leak = out.copy()
    leak[0, 0] = (leak[0, 0].astype(np.int64) + 5000) % 65535
    bad = gating.run_gates(plate, leak, None, None, "C", {"id": "fake"}, mask=mask, registration=reg)
    assert bad.verdict == "fail" and bad.metrics["outside_mask"]["n_changed_outside"] == 1

    unmeasured = gating.run_gates(
        plate, out, None, None, "C", None, mask=mask, registration={"shift_px": None}
    )
    assert {c.metric_id: c.status for c in unmeasured.checks}["registration.residual_px"] == "fail"
    assert gating.score_of(unmeasured) < gating.score_of(rep)
