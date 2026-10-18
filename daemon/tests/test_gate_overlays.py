"""Gate overlays reach the viewer (router plan T109, verification gap G1).

At gate time `gating.run_gates` stores, in the sink it is given (the run's project CAS once `gating.install` wraps
`Pipeline.gate`), the G-buffer edge masks edge recall scored (`edges_plate`, `edges_candidate`: 8-bit grey PNGs,
255 = edge) and the per-pixel dE00 map (`de00_map`: raw little-endian R16F at the output's size), and lists them
under `metrics.overlays`; `GET /runs/{id}/gate` lifts the block to a top-level `overlays`, and `GET /assets/{hash}`
serves the bytes the viewer's `e` and `h` modes load. A report written without them answers `overlays: null`.

Real pixels: the committed Blender packet `fixtures/packets/room_a` (skipped until it is built) and the
`window_added` synthetic. The 4K budget reuses the 3840 x 2160 packet builder of `gates/tests/test_perf_4k.py`
(room_a, EXR layers nearest x6) and is skipped under CI=1, as that test is.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from fixtures.synthetic import make_pair

from plate_daemon import gating
from plate_daemon.api import create_app
from plate_daemon.cas import CAS, hash_bytes
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.gate_reports import Service
from plate_gates import gbuffer
from plate_gates.color import ciede2000, srgb_to_lab
from plate_gates.io import to_uint8

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
PERF_4K = ROOT / "gates" / "tests" / "test_perf_4k.py"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
REG_OK = {"shift_px": 0.0, "residual_px": 0.1, "registered": True, "camera_drift": False}
WALL_BUDGET_S = 10.0  # T67's 4K gate budget, overlays included
OVERLAY_BUDGET_S = 1.5  # the overlays' own share of it at 4K
HALF_TOL = 0.02  # binary16 against float64 CIEDE2000 at the few-dE00 values here

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


class MemSink:
    """An overlay sink that keeps what it was given, keyed as the CAS keys it."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}

    def put_bytes(self, b: bytes, ext: str | None = None) -> str:
        key = hash_bytes(b)
        self.objects[key] = (b, ext)
        return key

    def read(self, key: str) -> bytes:
        return self.objects[key][0]


class BrokenSink:
    def put_bytes(self, b: bytes, ext: str | None = None) -> str:
        raise OSError("disk full")


def _png(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    assert img is not None
    return img


def _r16f(data: bytes, w: int, h: int) -> np.ndarray:
    assert len(data) == w * h * 2  # what the viewer's loadHeat accepts as R16F
    return np.frombuffer(data, dtype="<f2").astype(np.float32).reshape(h, w)


def _de00_ref(plate16: np.ndarray, out16: np.ndarray) -> np.ndarray:
    to8 = 255.0 / 65535.0
    return ciede2000(
        srgb_to_lab(plate16.astype(np.float64) * to8), srgb_to_lab(out16.astype(np.float64) * to8)
    )


# ---------------------------------------------------------------------------- run_gates on room_a


@pytest.fixture(scope="module")
def room_a():
    if not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file():
        pytest.skip("fixtures/packets/room_a not built yet (T2)")
    from plate_pixels import imageio
    from plate_pixels.exr import read_cryptomatte

    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    plate = np.rint(imageio.read(ROOM_A / "display.tif").rgb * 65535).astype(np.uint16)
    legend = json.loads((ROOM_A / "id_legend.json").read_text(encoding="utf-8"))
    edges = gbuffer.from_packet(ROOM_A, manifest=manifest)
    material = read_cryptomatte(ROOM_A / manifest["files"]["beauty_exr"]["path"], manifest, "material")
    return {"plate": plate, "legend": legend, "edges": edges, "material": material, "manifest": manifest}


def _run(room, out, mask=None, **kw):
    return gating.run_gates(
        room["plate"],
        out,
        {"path": ROOM_A, "manifest": room["manifest"]},
        room["legend"],
        "A",
        {"id": "echo", "version": "t109"},
        mask=mask,
        registration=REG_OK,
        edges=room["edges"],
        crypto_material=room["material"],
        **kw,
    )


def _box_edit(room, amount: int = 1500):
    """A box mask and an output brightened inside it only (a composite: identical outside by construction)."""
    mask = np.zeros(room["plate"].shape[:2], dtype=bool)
    mask[40:200, 300:600] = True
    out = room["plate"].copy()
    out[mask] = np.clip(out[mask].astype(np.int64) + amount, 0, 65535).astype(np.uint16)
    return mask, out


def test_run_gates_stores_the_scored_edge_masks_and_the_de00_map(room_a):
    sink = MemSink()
    mask, out = _box_edit(room_a)
    report = _run(room_a, out, mask, overlay_sink=sink)
    h, w = room_a["plate"].shape[:2]
    block = report.metrics["overlays"]
    assert block["schema"] == gating.OVERLAYS_SCHEMA and (block["width"], block["height"]) == (w, h)
    assert block["notes"] == []  # 640 px is under the working width: the masks are at the output's size
    assert sorted(ext for _b, ext in sink.objects.values()) == ["png", "png", "r16f"]

    emap, _ctx = room_a["edges"]
    plate_e, cand_e = block["edges_plate"], block["edges_candidate"]
    for entry in (plate_e, cand_e):
        assert entry["format"] == gating.EDGE_FORMAT == "png8" and entry["media_type"] == "image/png"
        assert (entry["width"], entry["height"]) == (w, h) == (emap.width, emap.height)
    ref_png = _png(sink.read(plate_e["hash"]))
    assert ref_png.dtype == np.uint8 and ref_png.shape == (h, w)  # single channel, 8-bit
    assert set(np.unique(ref_png)) <= {0, 255}
    # exactly the reference edges the recall gate scored, and the candidate edges it detected
    assert np.array_equal(ref_png == 255, emap.edges)
    assert plate_e["edge_px"] == report.metrics["gbuffer"]["n_ref"] == int(emap.edges.sum())
    cand_png = _png(sink.read(cand_e["hash"]))
    want = gbuffer.test_edges(gbuffer.resample_rgb(to_uint8(out), w, h))
    assert np.array_equal(cand_png == 255, want)
    assert cand_e["edge_px"] == report.metrics["gbuffer"]["n_test"]
    assert cand_e["source"] == report.metrics["gbuffer"]["backend"]

    de = block["de00_map"]
    assert de["format"] == gating.DE00_FORMAT == "r16f" and de["media_type"] == "application/octet-stream"
    assert (de["width"], de["height"], de["unit"], de["scale"]) == (w, h, "dE00", 1.0)
    got = _r16f(sink.read(de["hash"]), w, h)
    assert np.all(got[~mask] == 0.0)  # outside the mask the composite is the plate, bit for bit
    ref = _de00_ref(room_a["plate"][mask], out[mask])
    assert np.max(np.abs(got[mask] - ref)) <= HALF_TOL
    assert de["changed_px"] == int(np.any(out != room_a["plate"], axis=-1).sum())
    assert de["max"] == pytest.approx(float(ref.max()), abs=HALF_TOL)
    assert de["max"] > 1.0  # the edit is visible on the heatmap


def test_without_a_sink_nothing_is_computed_and_the_report_has_no_block(room_a, monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(gating, "_overlays", lambda *a, **k: called.append(1))
    mask, out = _box_edit(room_a)
    report = _run(room_a, out, mask)
    assert "overlays" not in report.metrics and called == []
    assert gating.overlays_of(report.model_dump(mode="json")) is None


def test_same_output_same_hashes_so_a_rescore_stores_nothing_new(room_a):
    mask, out = _box_edit(room_a)
    a, b = MemSink(), MemSink()
    first = _run(room_a, out, mask, overlay_sink=a).metrics["overlays"]
    again = _run(room_a, out, mask, overlay_sink=b).metrics["overlays"]
    for name in ("edges_plate", "edges_candidate", "de00_map"):
        assert first[name]["hash"] == again[name]["hash"]
    assert set(a.objects) == set(b.objects)


def test_a_failing_sink_is_a_note_never_a_failed_gate(room_a):
    mask, out = _box_edit(room_a)
    plain = _run(room_a, out, mask)
    broken = _run(room_a, out, mask, overlay_sink=BrokenSink())
    assert broken.verdict == plain.verdict
    assert [c.status for c in broken.checks] == [c.status for c in plain.checks]
    block = broken.metrics["overlays"]
    assert block["schema"] == gating.OVERLAYS_SCHEMA and "disk full" in block["error"]
    assert "edges_plate" not in block and "overlays: not stored (OSError)" in broken.backend_notes


# ---------------------------------------------------------------------------- tier C, the context sink, install


def _tier_c(sink=None, **kw):
    ref, test, mask, _expected = make_pair("window_added")
    return (
        ref,
        test,
        mask,
        gating.run_gates(
            ref,
            test,
            None,
            None,
            "C",
            {"id": "echo"},
            mask=mask,
            registration=REG_OK,
            overlay_sink=sink,
            **kw,
        ),
    )


def test_tier_c_stores_the_map_only():
    sink = MemSink()
    ref, test, mask, report = _tier_c(sink)
    block = report.metrics["overlays"]
    assert block["edges_plate"] is None and block["edges_candidate"] is None
    assert block["notes"] == ["edges: tier C has no G-buffer edge masks"]
    h, w = ref.shape[:2]
    got = _r16f(sink.read(block["de00_map"]["hash"]), w, h)
    want = np.zeros((h, w))
    changed = np.any(ref != test, axis=-1)
    want[changed] = _de00_ref(ref[changed].astype(np.uint16) * 257, test[changed].astype(np.uint16) * 257)
    assert np.max(np.abs(got - want)) <= HALF_TOL and got[changed].max() > 1.0
    assert np.all(got[~changed] == 0.0)


def test_overlays_to_binds_a_sink_for_the_block_and_an_explicit_sink_wins():
    bound, explicit = MemSink(), MemSink()
    with gating.overlays_to(bound):
        _r, _t, _m, inside = _tier_c()
        _r, _t, _m, given = _tier_c(explicit)
    _r, _t, _m, after = _tier_c()
    assert inside.metrics["overlays"]["de00_map"]["hash"] in bound.objects
    assert given.metrics["overlays"]["de00_map"]["hash"] in explicit.objects
    assert len(bound.objects) == 1  # the explicit sink took the second call's map
    assert "overlays" not in after.metrics  # the binding ends with the block


class _FakePipeline:
    """The one method `gating.install` wraps: `gate(run, cas)` calling run_gates as Pipeline.gate does."""

    def __init__(self) -> None:
        self.reports: list[Any] = []

    def gate(self, run: Any, cas: Any) -> Any:
        _r, _t, _m, report = _tier_c()
        self.reports.append(report)
        return report


def test_install_wraps_pipeline_gate_once_and_stores_in_the_runs_cas(tmp_path):
    pipe = _FakePipeline()
    assert gating.install(pipe) is pipe
    wrapped = pipe.gate
    gating.install(pipe)
    assert pipe.gate is wrapped  # idempotent
    cas = CAS(tmp_path / "assets")
    report = pipe.gate("run", cas)
    key = report.metrics["overlays"]["de00_map"]["hash"]
    assert cas.find(key) is not None and cas.find(key).name.endswith(".r16f")
    assert "overlays" not in _tier_c()[3].metrics  # nothing outside the wrapped call


# ---------------------------------------------------------------------------- the daemon: gate, rescore, assets


class TintEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent with a box in its centre brightened, so the gates and the
    overlays score real, changed pixels."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)  # call log, expiry
        img = _png(self.sent[result_url[len(RESULT_SCHEME) :]])
        h, w = img.shape[:2]
        box = img[h // 3 : h // 2, w // 3 : w // 2]
        step = 1500 if img.dtype == np.uint16 else 6
        top = np.iinfo(img.dtype).max
        img[h // 3 : h // 2, w // 3 : w // 2] = np.clip(box.astype(np.int64) + step, 0, top).astype(img.dtype)
        return bytes(cv2.imencode(".png", img)[1])


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    return Settings.load()


def _client(settings: Settings, engine) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _setup(c: TestClient, auth: dict) -> None:
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
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
    r = c.put(
        f"/api/v0/projects/{PROJECT}/graph",
        json={"nodes": nodes, "edges": [{"id": "p->e", "source": "p", "target": "e"}]},
        headers=auth,
    )
    assert r.status_code == 200, r.text


def _take(c: TestClient, auth: dict) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    run_id = r.json()["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
    assert run["state"] in ("DONE", "GATE_FAIL"), run.get("error")
    return run


def _asset(c: TestClient, auth: dict, key: str):
    r = c.get(f"/api/v0/assets/{key}", headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["x-plate-source"] == "cas"
    return r


@needs_room_a
def test_a_take_gated_by_the_daemon_lists_and_serves_its_overlays(sync_settings, auth):
    with _client(sync_settings, TintEngine()) as c:
        _setup(c, auth)
        run = _take(c, auth)
        r = c.get(f"/api/v0/runs/{run['id']}/gate", headers=auth)
        assert r.status_code == 200, r.text
        view = r.json()
        ov = view["overlays"]
        assert ov is not None and ov == view["metrics"]["overlays"]  # lifted from the stored report
        assert set(ov) >= {"schema", "width", "height", "edges_plate", "edges_candidate", "de00_map"}
        w, h = ov["width"], ov["height"]
        assert (w, h) == (640, 360)
        out = c.app.state.pipeline.cas_for(PROJECT).read_bytes(run["normalized"]["outputs"][0])
        assert _png(out).shape[:2] == (h, w)  # the gated output's size

        for name in ("edges_plate", "edges_candidate"):
            entry = ov[name]
            resp = _asset(c, auth, entry["hash"])
            assert resp.headers["content-type"] == "image/png" == entry["media_type"]
            mask = _png(resp.content)
            assert mask.shape == (entry["height"], entry["width"]) == (h, w)
            assert int((mask == 255).sum()) == entry["edge_px"] > 0
        assert ov["edges_plate"]["edge_px"] == view["metrics"]["gbuffer"]["n_ref"]
        assert ov["edges_candidate"]["edge_px"] == view["metrics"]["gbuffer"]["n_test"]

        entry = ov["de00_map"]
        resp = _asset(c, auth, entry["hash"])
        assert resp.headers["content-type"] == "application/octet-stream"
        de = _r16f(resp.content, entry["width"], entry["height"])
        assert (
            de.shape == (h, w) and de.max() > 1.0 and entry["max"] == pytest.approx(float(de.max()), abs=0.01)
        )
        assert int((de > 0).sum()) <= entry["changed_px"]
        # the run row's copy of the report carries the block too
        assert gating.overlays_of(run["gate_report"]) == ov


@needs_room_a
def test_a_rescore_stores_overlays_for_its_version(sync_settings, auth):
    with _client(sync_settings, TintEngine()) as c:
        _setup(c, auth)
        run = _take(c, auth)
        first = c.get(f"/api/v0/runs/{run['id']}/gate", headers=auth).json()
        r = c.post(f"/api/v0/runs/{run['id']}/gate/rescore", json={}, headers=auth)
        assert r.status_code == 200, r.text
        again = r.json()
        assert again["n"] == 2 and again["overlays"] is not None
        for name in (
            "edges_plate",
            "edges_candidate",
            "de00_map",
        ):  # the same output scores the same overlays
            assert again["overlays"][name]["hash"] == first["overlays"][name]["hash"]
        assert (
            c.get(f"/api/v0/runs/{run['id']}/gate?n=1", headers=auth).json()["overlays"] == first["overlays"]
        )


@needs_room_a
def test_a_run_gated_before_t109_answers_null_overlays(sync_settings, auth, monkeypatch):
    monkeypatch.setattr(gating, "install", lambda pipeline: pipeline)  # a daemon without the overlay wrapper
    with _client(sync_settings, TintEngine()) as c:
        _setup(c, auth)
        run = _take(c, auth)
        view = c.get(f"/api/v0/runs/{run['id']}/gate", headers=auth).json()
        assert view["overlays"] is None and "overlays" not in view["metrics"]
        stored = Service(sync_settings.projects_dir / PROJECT).get(run["id"], 1).report
        assert gating.overlays_of(stored) is None


# ---------------------------------------------------------------------------- the 4K budget


def _perf_4k_helpers():
    spec = importlib.util.spec_from_file_location("plate_t109_perf_4k", PERF_4K)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_4k_gate_with_its_overlays_stays_inside_the_budget(tmp_path, monkeypatch, capsys):
    if os.environ.get("CI", "").strip().lower() in ("1", "true", "yes"):
        pytest.skip("CI is set: the 4K gate budget is measured on the studio Mac, GitHub runners are slower")
    if not (ROOM_A / "beauty.exr").is_file():
        pytest.skip(f"fixture packet missing: {ROOM_A}")
    import OpenEXR

    perf = _perf_4k_helpers()
    before = OpenEXR.global_thread_count()
    OpenEXR.set_global_thread_count(os.cpu_count() or 1)
    try:
        perf._build_4k(tmp_path / "room_a_4k")
        small, big = perf._warm(ROOM_A), perf._warm(tmp_path / "room_a_4k")
    finally:
        OpenEXR.set_global_thread_count(before)
    out_small, mask_small = perf._composite(
        small["plate16"], perf._whole_frame(small["plate16"]), perf._panel(small), 4, 3
    )
    gating.run_gates(  # warms the imports and the lazily built kernels
        small["plate16"],
        out_small,
        {"path": small["root"], "manifest": small["manifest"]},
        small["legend"],
        "A",
        {"id": "perf-4k"},
        mask=mask_small,
        edges=small["edges"],
        crypto_material=(small["ranks"], small["cov"]),
        overlay_sink=MemSink(),
    )

    h, w = big["plate16"].shape[:2]
    assert (w, h) == (3840, 2160)
    out16, mask = perf._composite(big["plate16"], perf._whole_frame(big["plate16"]), perf._panel(big), 24, 12)
    spent: list[float] = []
    real = gating._overlays

    def timed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            return real(*args, **kwargs)
        finally:
            spent.append(time.perf_counter() - t0)

    monkeypatch.setattr(gating, "_overlays", timed)
    sink = MemSink()
    t0 = time.perf_counter()
    report = gating.run_gates(
        big["plate16"],
        out16,
        {"path": big["root"], "manifest": big["manifest"]},
        big["legend"],
        "A",
        {"id": "perf-4k"},
        mask=mask,
        edges=big["edges"],
        crypto_material=(big["ranks"], big["cov"]),
        overlay_sink=sink,
    )
    wall = time.perf_counter() - t0
    line = f"run_gates {w}x{h} with overlays: wall {wall:.2f}s overlays {spent[0]:.2f}s"
    with capsys.disabled():
        print(f"\n{line}")

    block = report.metrics["overlays"]
    emap, _ctx = big["edges"]
    # the edges stay at the size edge recall scored them (the 2048 working width), never resampled up
    assert (emap.width, emap.height) == (gbuffer.WORKING_WIDTH, 1152)
    assert (block["edges_plate"]["width"], block["edges_plate"]["height"]) == (emap.width, emap.height)
    assert _png(sink.read(block["edges_plate"]["hash"])).shape == (emap.height, emap.width)
    assert block["notes"] == [f"edges: scored at {emap.width}x{emap.height}, never resampled up to {w}x{h}"]
    de = block["de00_map"]
    assert (de["width"], de["height"]) == (w, h) and len(sink.read(de["hash"])) == w * h * 2
    assert spent[0] <= OVERLAY_BUDGET_S, line
    assert wall <= WALL_BUDGET_S, line
