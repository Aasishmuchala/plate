"""Run history, gate rescore, export through `export.export`, ledgered overrides and the seed's dev routes
behind `PLATE_DEV` (router plan T57), on the committed Blender packet `fixtures/packets/room_a`.

room_a declares a 2.39:1 safe frame (y 46, height 268) inside its 640 x 360 render, so an export writes that
band by default (router plan T114: a pixel crop of the composite); `test_export_safe_frame.py` covers the
framing itself.

`EchoEngine` is a FakeEngine whose result is the PNG egress sent it (optionally shifted), so the gates score
real pixels: an unshifted echo ends DONE, a 12 px shift ends GATE_FAIL on camera drift.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient

from plate_daemon import lineage
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.gate_reports import Service
from plate_daemon.ledger import Ledger
from plate_daemon.pipeline import decode_png16
from plate_daemon.sse import EventFilter

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
FAKE_ROUTE = {"engine_id": "fake", "provider": "fake", "region": "local"}
DEV_404 = {"error": "not_found", "detail": "dev route; set PLATE_DEV=1"}
BAND = {"x": 0, "y": 46, "width": 640, "height": 268}  # room_a's camera.safe_frame (T114)
ROWS = slice(BAND["y"], BAND["y"] + BAND["height"])

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
        super().fetch(result_url)  # call log, expiry
        data = self.sent[result_url[len(RESULT_SCHEME) :]]
        if self.shift_px:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
            data = bytes(cv2.imencode(".png", np.roll(img, self.shift_px, axis=1))[1])
        return data


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


@pytest.fixture
def dev_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_DEV", "1")
    settings = Settings.load()
    assert settings.dev is True
    return settings


def _client(settings: Settings, engine) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict, *, edits: tuple[str, ...] = ("e",)) -> str:
    """Project + room_a ingested + a graph of one Plate feeding each Edit node in `edits`. The packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
    r = c.post("/api/v0/packets", json=body, headers=hdr)
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    assert c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested"
    nodes = [{"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}]
    nodes += [{"id": n, "kind": "Edit", "label": n, "params": {"engine": "fake"}} for n in edits]
    edges = [{"id": f"p->{n}", "source": "p", "target": n} for n in edits]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _quote(c: TestClient, auth: dict, node: str = "e", **body) -> str:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/{node}/quote", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["run"]["id"]


def _run(c: TestClient, auth: dict, node: str = "e", **body) -> dict:
    """Quote, approve (auto, under the cap) and run one take of `node` to its end; the run view."""
    run_id = _quote(c, auth, node, **body)
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


def _records(settings: Settings, kind: str, run_id: str) -> list[dict]:
    led = Ledger(settings.projects_dir / PROJECT)
    assert led.verify() == (True, None)
    return [r for r in led.records() if r["kind"] == kind and r.get("run_id") == run_id]


def _export(c: TestClient, auth: dict, run_id: str, **body):
    return c.post(f"/api/v0/runs/{run_id}/export", json=body, headers=auth)


# ---------------------------------------------------------------------------- run history


@needs_room_a
def test_history_lists_newest_first_and_filters_by_node_and_state(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth, edits=("e", "f"))
        first = _run(c, auth, "e")
        assert first["state"] == "DONE", first.get("error")
        other = _quote(c, auth, "f")
        again = _quote(c, auth, "e")

        def ids(query: str = "") -> list[str]:
            r = c.get(f"/api/v0/projects/{PROJECT}/runs{query}", headers=auth)
            assert r.status_code == 200, r.text
            assert isinstance(r.json(), list)
            return [v["id"] for v in r.json()]

        assert ids() == [again, other, first["id"]]
        assert ids("?node_id=e") == [again, first["id"]]
        assert ids("?state=DONE") == [first["id"]]
        assert ids("?state=QUOTED&node_id=e") == [again]
        assert ids("?state=done,quoted") == [again, other, first["id"]]
        assert ids("?limit=1") == [again]
        assert ids("?node_id=absent") == []

        views = c.get(f"/api/v0/projects/{PROJECT}/runs?state=DONE", headers=auth).json()
        view = views[0]
        assert view == c.get(f"/api/v0/runs/{first['id']}", headers=auth).json()
        assert view["node_id"] == "e" and view["exportable"] is True
        assert "sibling_group" in view and view["sibling_group"] is None
        assert "subgraph" in view and view["subgraph"] == view["inputs"].get("subgraph")

        for bad in ("?limit=0", "?limit=1001", "?state=NOPE"):
            assert c.get(f"/api/v0/projects/{PROJECT}/runs{bad}", headers=auth).status_code == 422, bad
        r = c.get("/api/v0/projects/absent/runs", headers=auth)
        assert r.status_code == 404 and r.json()["error"] == "not_found"
        assert c.get(f"/api/v0/projects/{PROJECT}/runs").status_code == 401


@needs_room_a
def test_best_of_n_siblings_carry_their_group_in_the_history(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)
        sibs = c.post(f"/api/v0/runs/{run_id}/fanout", json={"n": 2}, headers=auth).json()
        group = sibs[0]["sibling_group"]
        listed = c.get(f"/api/v0/projects/{PROJECT}/runs?node_id=e", headers=auth).json()
        assert sorted(v["id"] for v in listed) == sorted(s["id"] for s in sibs)
        assert {v["sibling_group"] for v in listed} == {group} and group is not None


# ---------------------------------------------------------------------------- gate rescore


@needs_room_a
def test_rescore_writes_the_next_version_and_never_touches_a_shipped_one(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        quoted = _quote(c, auth)
        r = c.post(f"/api/v0/runs/{quoted}/gate/rescore", json={}, headers=auth)
        assert r.status_code == 409 and r.json()["error"] == "no_report"
        assert c.post("/api/v0/runs/nope/gate/rescore", json={}, headers=auth).status_code == 404

        run = _run(c, auth)
        run_id = run["id"]
        assert run["state"] == "DONE", run.get("error")
        r = _export(c, auth, run_id, out_dir=str(tmp_path / "out"), format="png16", name="take")
        assert r.status_code == 200 and r.json()["shipped"] is True, r.text

        svc = Service(sync_settings.projects_dir / PROJECT)
        first = svc.get(run_id, 1)
        shipped_bytes = first.path.read_bytes()
        assert first.shipped

        r = c.post(f"/api/v0/runs/{run_id}/gate/rescore", headers=auth)  # no body is fine too
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["n"] == 2 and view["versions"] == [1, 2] and view["path"] == f"gates/{run_id}.2.json"
        assert view["rescored_from"] == 1 and view["rescored_from_shipped"] is True
        assert view["shipped"] is False
        assert first.path.read_bytes() == shipped_bytes and svc.verify_shipped(run_id, 1) is True
        # the same take scored by the same code, with the arguments Pipeline.gate passes, scores the same
        old = {ch["metric_id"]: (ch["status"], ch["value"]) for ch in first.report["checks"]}
        new = {ch["metric_id"]: (ch["status"], ch["value"]) for ch in view["checks"]}
        assert new == old and view["verdict"] == first.report["verdict"]
        assert view["metrics"]["output"] == run["outputs"][0]

        latest = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()
        assert latest["n"] == 2 and latest["shipped"] is False and latest["rescored_from"] == 1
        assert c.get(f"/api/v0/runs/{run_id}/gate?n=1", headers=auth).json()["shipped"] is True

        r = c.post(f"/api/v0/runs/{run_id}/gate/rescore", json={}, headers=auth)
        assert r.json()["n"] == 3 and r.json()["rescored_from"] == 2
        assert "rescored_from_shipped" not in r.json()
        assert first.path.read_bytes() == shipped_bytes
        r = c.post(f"/api/v0/runs/{run_id}/gate/rescore", json={"extra": 1}, headers=auth)
        assert r.status_code == 422


# ---------------------------------------------------------------------------- export through export.export


@needs_room_a
def test_export_png16_writes_the_composite_and_its_sidecar_and_ships_the_report(
    sync_settings, auth, tmp_path
):
    with _client(sync_settings, EchoEngine()) as c:
        packet_id = _setup(c, auth)
        run = _run(c, auth)
        run_id = run["id"]
        assert run["state"] == "DONE", run.get("error")
        out = tmp_path / "exports"
        r = _export(c, auth, run_id, out_dir=str(out), format="png16", name="take_01")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "run_id": run_id,
            "path": str(out / "take_01.png"),
            "sidecar": str(out / "take_01.gate.json"),
            "format": "png16",
            "shipped": True,
            "crop": {"x": 0, "y": 46, "width": 640, "height": 268},  # the route reports its framing (G3)
        }

        img = cv2.imread(str(out / "take_01.png"), cv2.IMREAD_UNCHANGED)
        assert img.dtype == np.uint16 and img.shape == (268, 640, 3)  # the safe frame (T114)
        cas = CAS(sync_settings.projects_dir / PROJECT / "assets")
        composite = decode_png16(cas.read_bytes(run["outputs"][0]))
        assert composite.shape == (360, 640, 3)
        assert np.array_equal(np.ascontiguousarray(img[..., ::-1]), composite[ROWS])
        lin = lineage.read(out / "take_01.png")
        assert lin is not None and lin.run_id == run_id and lin.packet_id == packet_id and lin.node_id == "e"
        sidecar = json.loads((out / "take_01.gate.json").read_text(encoding="utf-8"))
        assert sidecar["export"]["run_id"] == run_id and sidecar["export"]["files"] == ["take_01.png"]
        assert sidecar["export"]["gate_report"] == f"gates/{run_id}.1.json"
        assert sidecar["export"]["crop"] == BAND and "full_frame" not in sidecar["export"]
        assert sidecar["verdict"] == c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()["verdict"]

        gate = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()
        assert gate["shipped"] is True and gate["n"] == 1

        (rec,) = _records(sync_settings, "export", run_id)
        data = rec["data"]
        assert data["dest_name"] == "take_01.png" and data["format"] == "png16"
        assert data["sidecar"] == "take_01.gate.json" and data["by"] == "human"
        assert data["override_reason"] is None
        assert _records(sync_settings, "override", run_id) == []

        # never overwrites; a second format of the same take shares the sidecar
        r = _export(c, auth, run_id, out_dir=str(out), format="png16", name="take_01")
        assert r.status_code == 409 and r.json()["error"] == "not_exportable"
        r = _export(c, auth, run_id, out_dir=str(out), format="exr", name="take_01")
        assert r.status_code == 200, r.text
        sidecar = json.loads((out / "take_01.gate.json").read_text(encoding="utf-8"))
        assert sidecar["export"]["files"] == ["take_01.png", "take_01.exr"]

        # out_dir with no format or name: tiff16 under the run id
        r = _export(c, auth, run_id, out_dir=str(tmp_path / "plain"))
        assert r.status_code == 200 and r.json()["path"] == str(tmp_path / "plain" / f"{run_id}.tif")


@needs_room_a
def test_the_legacy_dest_body_writes_a_real_tiff_and_bad_bodies_are_refused(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        run = _run(c, auth)
        run_id = run["id"]
        dest = tmp_path / "legacy" / "plate_take.tif"
        r = _export(c, auth, run_id, dest=str(dest))
        assert r.status_code == 200, r.text
        assert r.json()["format"] == "tiff16" and r.json()["path"] == str(dest)
        tif = tifffile.imread(dest)
        assert tif.dtype == np.uint16 and tif.shape == (268, 640, 3)  # the safe frame (T114)
        cas = CAS(sync_settings.projects_dir / PROJECT / "assets")
        assert np.array_equal(tif, decode_png16(cas.read_bytes(run["outputs"][0]))[ROWS])
        assert (dest.parent / "plate_take.gate.json").is_file()
        assert lineage.read(dest).run_id == run_id

        png = tmp_path / "legacy" / "take.png"
        r = _export(c, auth, run_id, dest=str(png))
        assert r.status_code == 200 and r.json()["format"] == "png16"
        assert cv2.imread(str(png), cv2.IMREAD_UNCHANGED).dtype == np.uint16

        def refused(status: int, error: str | None = None, **body) -> None:
            r = _export(c, auth, run_id, **body)
            assert r.status_code == status, (body, r.text)
            if error is not None:
                assert r.json()["error"] == error, (body, r.json())

        refused(422, "bad_format", dest=str(tmp_path / "take.jpg"))
        refused(422, "bad_format", dest=str(tmp_path / "take"))
        refused(422, "bad_format", dest=str(tmp_path / "take.png"), format="tiff16")
        refused(422, "bad_format", out_dir=str(tmp_path), format="jpeg")
        refused(422, "bad_name", out_dir=str(tmp_path), name="my take")
        refused(422, "bad_name", dest=str(tmp_path / "my take.tif"))
        refused(422, dest=str(tmp_path / "a.tif"), name="b")
        refused(422, dest="relative.tif")
        refused(422, out_dir="relative")
        refused(422)
        refused(422, dest=str(tmp_path / "a.tif"), out_dir=str(tmp_path))
        refused(422, out_dir=str(tmp_path), colour="srgb")
        refused(409, "not_exportable", dest=str(dest))  # exists: exports never overwrite
        assert _export(c, auth, "nope", dest=str(tmp_path / "x.tif")).status_code == 404


# ---------------------------------------------------------------------------- overrides


@needs_room_a
def test_a_gate_fail_exports_only_on_an_override_and_the_typed_one_is_ledgered(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine(shift_px=12)) as c:
        _setup(c, auth)
        run = _run(c, auth)
        run_id = run["id"]
        assert run["state"] == "GATE_FAIL" and run["exportable"] is False
        out = str(tmp_path / "out")
        for body in (
            {},
            {"reason": "a reason with no override flag"},
            {"override": True},
            {"override": True, "reason": "too short"},
        ):
            r = _export(c, auth, run_id, out_dir=out, **body)
            assert r.status_code == 409 and r.json()["error"] == "not_exportable", (body, r.text)
        assert _records(sync_settings, "override", run_id) == []
        assert _records(sync_settings, "export", run_id) == []
        assert not Path(out).exists() or not any(Path(out).iterdir())

        reason = "ok by client"  # 12 characters
        r = _export(c, auth, run_id, out_dir=out, format="png16", override=True, reason=reason)
        assert r.status_code == 200, r.text
        (ovr,) = _records(sync_settings, "override", run_id)
        assert ovr["reason"] == reason and ovr["by"] == "human"
        (exp,) = _records(sync_settings, "export", run_id)
        assert exp["data"]["override_reason"] == reason and exp["data"]["format"] == "png16"
        view = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert view["override"]["reason"] == reason and view["exportable"] is True
        sidecar = json.loads((Path(out) / f"{run_id}.gate.json").read_text(encoding="utf-8"))
        assert sidecar["export"]["override_reason"] == reason and sidecar["export"]["state"] == "GATE_FAIL"
        events = c.app.state.bus.read(EventFilter(PROJECT, run_id), 0)
        assert [e["data"]["reason"] for e in events if e["kind"] == "run.override"] == [reason]

        # the recorded override lets later exports out without typing it again, and adds no override record
        r = _export(c, auth, run_id, out_dir=str(tmp_path / "again"))
        assert r.status_code == 200, r.text
        assert len(_records(sync_settings, "override", run_id)) == 1
        assert _records(sync_settings, "export", run_id)[-1]["data"]["override_reason"] == reason


@needs_room_a
def test_the_override_route_and_a_new_typed_reason_are_both_ledgered(sync_settings, auth, tmp_path):
    with _client(sync_settings, EchoEngine(shift_px=12)) as c:
        _setup(c, auth)
        run_id = _run(c, auth)["id"]
        reason = "client approved the shifted crop"
        r = c.post(f"/api/v0/runs/{run_id}/override", json={"reason": reason, "by": "human"}, headers=auth)
        assert r.status_code == 200 and r.json()["exportable"] is True, r.text
        (ovr,) = _records(sync_settings, "override", run_id)
        assert ovr["reason"] == reason and ovr["by"] == "human"

        r = _export(c, auth, run_id, out_dir=str(tmp_path / "a"))
        assert r.status_code == 200, r.text
        assert len(_records(sync_settings, "override", run_id)) == 1
        assert _records(sync_settings, "export", run_id)[-1]["data"]["override_reason"] == reason

        typed = "the client signed the drifted frame off"
        r = _export(c, auth, run_id, out_dir=str(tmp_path / "b"), override=True, reason=typed)
        assert r.status_code == 200, r.text
        assert [o["reason"] for o in _records(sync_settings, "override", run_id)] == [reason, typed]
        assert _records(sync_settings, "export", run_id)[-1]["data"]["override_reason"] == typed
        assert c.get(f"/api/v0/runs/{run_id}", headers=auth).json()["override"]["reason"] == typed


# ---------------------------------------------------------------------------- the seed's dev routes


def test_dev_routes_are_not_routes_without_plate_dev(settings, auth):
    assert settings.dev is False
    with TestClient(create_app(settings, FakeEngine()), base_url=BASE) as c:
        body = {"project_id": "p", "packet_id": "k", "route": FAKE_ROUTE}
        r = c.post("/api/v0/runs", json=body, headers=auth)
        assert r.status_code == 404 and r.json() == DEV_404
        r = c.post("/api/v0/runs", json={"not": "a run"}, headers=auth)  # refused before the body is read
        assert r.status_code == 404 and r.json() == DEV_404
        for step in ("quote", "queue", "step"):
            r = c.post(f"/api/v0/runs/anything/{step}", headers=auth)
            assert r.status_code == 404 and r.json() == DEV_404, step
        assert c.post("/api/v0/runs", json=body).status_code == 401


@needs_room_a
def test_dev_routes_drive_a_seed_run_under_plate_dev(dev_settings, auth, tmp_path):
    with _client(dev_settings, FakeEngine()) as c:
        packet_id = _setup(c, auth)
        base = {"project_id": PROJECT, "packet_id": packet_id, "route": FAKE_ROUTE}
        for key, value in (("staging", {}), ("mask", {"kind": "frame"}), ("plate", "0" * 64)):
            r = c.post("/api/v0/runs", json={**base, "inputs": {key: value}}, headers=auth)
            assert r.status_code == 422 and r.json()["error"] == "dev_route_inputs", key
            assert r.json()["inputs"] == [key]
        with c.app.state.index.project(PROJECT) as conn:
            assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0

        r = c.post("/api/v0/runs", json={**base, "inputs": {"prompt": "dusk"}}, headers=auth)
        assert r.status_code == 201, r.text
        run_id = r.json()["id"]
        assert c.post(f"/api/v0/runs/{run_id}/quote", headers=auth).status_code == 200
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"actor": "human"}, headers=auth)
        assert r.status_code == 200 and r.json()["state"] == "APPROVED"
        assert c.post(f"/api/v0/runs/{run_id}/queue", headers=auth).json()["state"] == "QUEUED"
        state = "QUEUED"
        for _ in range(20):
            state = c.post(f"/api/v0/runs/{run_id}/step", headers=auth).json()["state"]
            if state in ("DONE", "GATE_FAIL", "FAILED"):
                break
        assert state == "DONE"
        assert [v["id"] for v in c.get(f"/api/v0/projects/{PROJECT}/runs", headers=auth).json()] == [run_id]

        # a seed run exports through export.export too. Its 64 x 64 stub take is not room_a's aspect, so the
        # safe frame cannot be placed on it (T114): the default export refuses, and the whole take leaves only
        # on request (`full_frame`, which the batch route takes). Its stub gate wrote no report file to ship.
        dest = tmp_path / "seed.png"
        r = _export(c, auth, run_id, dest=str(dest))
        assert r.status_code == 409 and r.json()["error"] == "not_exportable", r.text
        assert "full_frame" in r.json()["detail"] and not dest.exists()
        body = {"run_ids": [run_id], "out_dir": str(tmp_path), "format": "png16", "name_pattern": "seed"}
        r = c.post(f"/api/v0/projects/{PROJECT}/export", json={**body, "full_frame": True}, headers=auth)
        assert r.status_code == 200 and r.json()["refused"] == [], r.text
        assert r.json()["exported"] == [
            {
                "run_id": run_id,
                "path": str(dest),
                "sidecar": str(tmp_path / "seed.gate.json"),
                "full_frame": True,
            }
        ]
        assert dest.is_file() and (tmp_path / "seed.gate.json").is_file()
        assert cv2.imread(str(dest), cv2.IMREAD_UNCHANGED).shape[:2] == (64, 64)
        assert lineage.read(dest).run_id == run_id
        rec = _records(dev_settings, "export", run_id)[-1]
        assert rec["data"]["shipped"] is False and rec["data"]["full_frame"] is True

        r = c.post(f"/api/v0/runs/{run_id}/gate/rescore", json={}, headers=auth)
        assert r.status_code == 409 and r.json()["error"] == "no_report"
        Service(dev_settings.projects_dir / PROJECT).write(
            run_id, {"gate_version": "stub-0", "metric_code_hash": "x", "threshold_set_id": "y"}
        )
        r = c.post(f"/api/v0/runs/{run_id}/gate/rescore", json={}, headers=auth)
        assert r.status_code == 409 and r.json()["error"] == "not_rescorable"
