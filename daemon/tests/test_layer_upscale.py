"""AI-layer upscale at ingress (router plan T100, decision 55), on the committed Blender packet `room_a`.

An engine that answers at half the plate region's size gets its take upscaled before ingress maps it back: by
Real-ESRGAN, whose stub on this Mac is a Lanczos resample flagged `stub: true` (so an echo still passes its gates),
or by Magnific when, and only when, the quote pre-declared it, custody allows it and PLATE_LIVE=1. `HalfEcho` is the
echoing FakeEngine that hands back the PNG it was sent at half its size (what a 1K tier does to a 2K plate) and
keeps a journal of its payloads. `FakeMagnific` stands in for the Magnific adapter (nothing leaves the box here).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import layer_upscale
from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.custody import CustodyViolation
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.machine import get_run
from plate_daemon.pipeline import PipelineError
from plate_engines.adapters.base import JobStatus, PlateUpscaleForbidden

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
SKY = "o:929c9e95"  # editable on room_a, smooth: a half-size round trip keeps its structure
FAKE_PRICE = 0.04
MAGNIFIC_PRICE = 0.32  # the manifest's per-image figure (unconfirmed)

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return bytes(buf)


def _img(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)


class HalfEcho(FakeEngine):
    """The echoing FakeEngine answering at 1/`div` of the size it was sent; `payloads` keeps every submit."""

    def __init__(self, *args, div: int = 2, **kwargs):
        kwargs.setdefault("echo", True)
        super().__init__(*args, **kwargs)
        self.div = div
        self.payloads: list[dict] = []

    def submit(self, payload, idempotency_key):
        self.payloads.append(copy.deepcopy(payload))
        return super().submit(payload, idempotency_key)

    def fetch(self, result_url):
        data = super().fetch(result_url)
        if self.div == 1:
            return data
        img = _img(data)
        h, w = img.shape[:2]
        return _png(cv2.resize(img, (w // self.div, h // self.div), interpolation=cv2.INTER_AREA))


class FakeMagnific:
    """What `LayerUpscaler.magnific_factory` builds: the adapter's submit/poll/fetch on the real class's quote and
    params checks, upscaling with Lanczos. `fail` makes the vendor fail the job."""

    def __init__(self, manifest, route, *, fail: bool = False):
        from plate_engines.adapters.upscale import MagnificPrecisionV2

        self.real = MagnificPrecisionV2(manifest, route, transport=_NoNetwork())
        self.fail = fail
        self.submitted: list = []
        self._out: dict[str, bytes] = {}

    def quote(self, inputs, params):
        return self.real.quote(inputs, params)

    def validate_params(self, params):
        return self.real.validate_params(params)

    def submit(self, staged):
        self.submitted.append(staged)
        img = staged.inputs["image"]
        if img.get("role") == "plate":
            raise PlateUpscaleForbidden("the plate is never upscaled")
        arr = _img(img["bytes"])
        s = int(staged.params["scale"])
        job = f"mag-{len(self.submitted)}"
        self._out[job] = _png(
            cv2.resize(arr, (arr.shape[1] * s, arr.shape[0] * s), interpolation=cv2.INTER_LANCZOS4)
        )
        return job

    def poll(self, job):
        if self.fail:
            return JobStatus(state="failed", message="magnific FAILED: the vendor gave up")
        return JobStatus(state="done", outputs=[f"https://ai-statics.freepik.com/up/{job}.png"])

    def fetch(self, url):
        return self._out[url.rsplit("/", 1)[1][: -len(".png")]]


class _NoNetwork:
    """A transport that must never be used (the fake answers everything itself)."""

    def handle_request(self, request):  # pragma: no cover - a request here is the bug
        raise AssertionError(f"no request may leave: {request.url}")


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    for name in ("PLATE_ROUTER_MODE", "PLATE_LIVE", "PLATE_STUB_LOCAL"):
        monkeypatch.delenv(name, raising=False)
    return Settings.load()


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine or HalfEcho()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _ingest(c: TestClient, auth: dict, packet: Path) -> str:
    manifest = json.loads((packet / "manifest.json").read_text(encoding="utf-8"))
    hdr = {**auth, "X-Plate-Packet-Version": manifest["packet_version"]}
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(packet), "sender": {"name": "test", "version": "0"}},
        headers=hdr,
    )
    assert r.status_code == 202, r.text
    return r.json()["packet_id"]


def _setup(c: TestClient, auth: dict, *, nda: bool = False, edit_params: dict | None = None) -> str:
    """The project, room_a, and Plate -> Mask(SKY) -> Edit(fake). Returns the packet id."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=auth)
    assert r.status_code == 201, r.text
    packet_id = _ingest(c, auth, ROOM_A)
    nodes = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        {"id": "m", "kind": "Mask", "params": {"ids": [SKY], "textLock": True}},
        {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake", **(edit_params or {})}},
    ]
    edges = [{"id": "p->m", "source": "p", "target": "m"}, {"id": "m->e", "source": "m", "target": "e"}]
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes, "edges": edges}, headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _quote(c: TestClient, body: dict):
    with c.app.state.index.project(PROJECT) as conn:
        return c.app.state.pipeline.quote_node(conn, PROJECT, "e", body)


def _approve(c: TestClient, auth: dict, run_id: str) -> dict:
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth)
    assert r.status_code == 200, r.text
    return c.get(f"/api/v0/runs/{run_id}", headers=auth).json()


def _run_count(c: TestClient) -> int:
    with c.app.state.index.project(PROJECT) as conn:
        return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def _cas(settings: Settings) -> CAS:
    return CAS(settings.projects_dir / PROJECT / "assets")


def _records(settings: Settings, run_id: str) -> list[dict]:
    return [r for r in Ledger(settings.projects_dir / PROJECT).records() if r.get("run_id") == run_id]


def _assert_passed(gate: dict) -> None:
    """No check failed: the verdict is pass, or advisory (the feather band stays advisory until calibration)."""
    assert gate["verdict"] in ("pass", "advisory"), gate["verdict"]
    assert [ch["metric_id"] for ch in gate["checks"] if ch["status"] == "fail"] == []
    assert any(
        ch["metric_id"] == "gbuffer_edge_recall_r2" and ch["status"] == "pass" for ch in gate["checks"]
    )


def _hook_record(done: dict) -> dict:
    return (done["normalized"].get("ingress_hooks") or {}).get(layer_upscale.HOOK_NAME)


# ---------------------------------------------------------------------------- the factor (pure)


@pytest.mark.parametrize(
    ("result", "target", "scale_range", "cap", "want"),
    [
        ((320, 180), (640, 360), (1, 4), None, 2),  # half size: 2x
        ((640, 360), (640, 360), (1, 4), None, None),  # the size it was sent: nothing to do
        ((540, 304), (640, 360), (1, 4), None, None),  # 1.18x short: ingress's Lanczos is enough
        ((512, 288), (640, 360), (1, 4), None, 2),  # exactly 1.25x short: acts, at the next whole factor
        ((640, 200), (640, 360), (1, 4), None, 2),  # only the height is short
        ((256, 144), (640, 360), (1, 4), None, 3),  # 2.5x -> 3
        ((64, 64), (640, 360), (1, 4), None, 4),  # 10x, clamped to the engine's range
        ((64, 36), (640, 360), (2, 16), None, 10),  # Magnific's documented range goes further
        ((4000, 3000), (16000, 12000), (1, 4), 50.0, 2),  # 4x would be 192 MP; 2x is 48 MP
        ((6000, 5000), (16000, 12000), (1, 4), 50.0, None),  # even 2x is over 50 MP
    ],
)
def test_the_factor_is_the_whole_ratio_clamped_to_the_range_and_the_output_cap(
    result, target, scale_range, cap, want
):
    assert layer_upscale.plan_factor(result, target, scale_range, cap) == want


# ---------------------------------------------------------------------------- auto: Real-ESRGAN, stubbed here


def test_a_half_size_take_is_upscaled_2x_by_the_lanczos_stub_and_still_passes_its_gate(sync_settings, auth):
    with _client(sync_settings) as c:
        up = layer_upscale.install(c.app.state.pipeline)
        assert layer_upscale.install(c.app.state.pipeline) is up  # idempotent
        names = [n for n, _ in c.app.state.pipeline.ingress_hooks]
        assert names.count(layer_upscale.HOOK_NAME) == 1
        _setup(c, auth)
        run, quote = _quote(c, {})
        assert quote.price_usd == pytest.approx(FAKE_PRICE) and "upscale" not in run.inputs  # auto is free
        done = _approve(c, auth, run.id)
        assert done["state"] == "DONE", done.get("error")

        rec = _hook_record(done)
        assert rec["engine_id"] == "real-esrgan" and rec["factor"] == 2 and rec["stub"] is True
        assert rec["mode"] == "auto" and rec["reason"]  # why the model did not run (no gpu extra here)
        assert rec["result_size"] == [320, 180] and rec["target"] == [640, 360]
        assert rec["upscaled_size"] == [640, 360]
        # Ingress now has nothing left to resample.
        assert done["normalized"]["ingress"]["result_size"] == [640, 360]
        assert done["normalized"]["ingress"]["resample"] == "none"

        # The stub is a Lanczos resample of the raw take, within 2 LSB.
        cas = _cas(sync_settings)
        raw = _img(cas.read_bytes(done["normalized"]["raw_key"]))
        assert raw.shape[:2] == (180, 320)  # the raw take stays what the engine sent
        lanczos = cv2.resize(raw, (640, 360), interpolation=cv2.INTER_LANCZOS4).astype(np.int16)
        upscaled = _img(cas.read_bytes(rec["output_key"])).astype(np.int16)
        assert upscaled.shape == lanczos.shape
        assert int(np.abs(upscaled - lanczos).max()) <= 2

        gate = c.get(f"/api/v0/runs/{run.id}/gate", headers=auth).json()
        _assert_passed(gate)
        assert gate["metrics"]["outside_mask"]["identical_outside"] is True  # the plate is never resampled


def test_a_take_at_the_size_it_was_sent_is_left_alone(sync_settings, auth):
    with _client(sync_settings, HalfEcho(div=1)) as c:
        layer_upscale.install(c.app.state.pipeline)
        _setup(c, auth)
        run, _q = _quote(c, {})
        done = _approve(c, auth, run.id)
        assert done["state"] == "DONE", done.get("error")
        assert _hook_record(done) == {}
        assert done["normalized"]["ingress"]["result_size"] == [640, 360]


def test_off_records_nothing_and_never_reaches_the_engine(sync_settings, auth):
    engine = HalfEcho()
    with _client(sync_settings, engine) as c:
        layer_upscale.install(c.app.state.pipeline)
        _setup(c, auth)
        run, _q = _quote(c, {"params": {"upscale_layers": "off"}})
        assert run.params["upscale_layers"] == "off"  # PLATE's option, kept on the run
        done = _approve(c, auth, run.id)
        assert done["state"] == "DONE", done.get("error")
        assert not _hook_record(done)  # no record: ingress resampled the layer itself
        assert done["normalized"]["ingress"]["result_size"] == [320, 180]
        assert done["normalized"]["ingress"]["resample"] == "lanczos4"
        assert "upscale_layers" not in engine.payloads[-1]["params"]  # never sent to the vendor


def test_the_mode_can_come_from_the_node_and_a_bad_one_is_refused(sync_settings, auth):
    with _client(sync_settings) as c:
        layer_upscale.install(c.app.state.pipeline)
        _setup(c, auth, edit_params={"upscaleLayers": "off"})
        run, _q = _quote(c, {})
        assert run.params["upscale_layers"] == "off"
        with pytest.raises(PipelineError) as e:
            _quote(c, {"params": {"upscale_layers": "4x"}})
        assert (e.value.code, e.value.status) == ("bad_upscale_layers", 422)
        assert _run_count(c) == 1


# ---------------------------------------------------------------------------- magnific: declared, paid, allowed


def test_magnific_on_an_nda_project_is_refused_by_custody_before_any_run_row(
    sync_settings, auth, monkeypatch
):
    monkeypatch.setenv("PLATE_LIVE", "1")
    with _client(sync_settings) as c:
        layer_upscale.install(c.app.state.pipeline)
        _setup(c, auth, nda=True)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/nodes/e/quote",
            json={"params": {"upscale_layers": "magnific"}},
            headers=auth,
        )
        assert r.status_code == 403, r.text
        assert r.json()["error"] == "custody" and r.json()["grade"] == "D"
        monkeypatch.delenv("PLATE_LIVE")
        with pytest.raises(CustodyViolation):  # custody is asked first, live or not
            _quote(c, {"params": {"upscale_layers": "magnific"}})
        assert _run_count(c) == 0


def test_magnific_without_plate_live_is_refused_before_any_run_row(sync_settings, auth):
    with _client(sync_settings) as c:
        layer_upscale.install(c.app.state.pipeline)
        _setup(c, auth)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/nodes/e/quote",
            json={"params": {"upscale_layers": "magnific"}},
            headers=auth,
        )
        assert r.status_code == 422, r.text
        assert r.json()["error"] == "magnific_live_only"
        assert _run_count(c) == 0


def test_a_declared_magnific_pass_is_priced_on_the_quote_and_in_the_ledger(sync_settings, auth, monkeypatch):
    monkeypatch.setenv("PLATE_LIVE", "1")
    with _client(sync_settings) as c:
        layer_upscale.install(c.app.state.pipeline)
        _setup(c, auth)
        r = c.post(
            f"/api/v0/projects/{PROJECT}/nodes/e/quote",
            json={"params": {"upscale_layers": "magnific"}},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        quote, run = r.json()["quote"], r.json()["run"]
        assert quote["price_usd"] == pytest.approx(FAKE_PRICE + MAGNIFIC_PRICE)
        assert quote["price_status"] == "fake"  # the weaker of fake and unconfirmed
        assert run["inputs"]["upscale"] == {
            "engine_id": "magnific-precision-v2",
            "route_id": "magnific",
            "price_usd": MAGNIFIC_PRICE,
            "price_status": "unconfirmed",
        }
        with c.app.state.index.project(PROJECT) as conn:
            stored = get_run(conn, run["id"])
            row = conn.execute("SELECT price_usd FROM quotes WHERE id = ?", (stored.quote.id,)).fetchone()
        assert stored.quote.price_usd == pytest.approx(FAKE_PRICE + MAGNIFIC_PRICE)
        assert row[0] == pytest.approx(FAKE_PRICE + MAGNIFIC_PRICE)  # what approval reserves
        quoted = [x for x in _records(sync_settings, run["id"]) if x["kind"] == "quoted"]
        assert len(quoted) == 1
        assert quoted[0]["cost_usd_quoted"] == pytest.approx(FAKE_PRICE + MAGNIFIC_PRICE)
        assert quoted[0]["data"]["inputs"]["upscale"]["engine_id"] == "magnific-precision-v2"
        assert quoted[0]["data"]["quote"]["price_usd"] == pytest.approx(FAKE_PRICE + MAGNIFIC_PRICE)

        # The same request again is attempt 2 (numbered against the declared inputs, so no key collision);
        # an auto quote of the node is another request.
        again, _q = _quote(c, {"params": {"upscale_layers": "magnific"}})
        auto, _q = _quote(c, {})
        assert (run["attempt"], again.attempt, auto.attempt) == (1, 2, 1)


def test_a_declared_magnific_pass_runs_on_the_ai_layer_never_the_plate(sync_settings, auth, monkeypatch):
    monkeypatch.setenv("PLATE_LIVE", "1")
    with _client(sync_settings) as c:
        up = layer_upscale.install(c.app.state.pipeline)
        made: list[FakeMagnific] = []
        up.magnific_factory = lambda run, manifest, route: (
            made.append(FakeMagnific(manifest, route)) or made[-1]
        )
        _setup(c, auth)
        run, _q = _quote(c, {"params": {"upscale_layers": "magnific"}})
        done = _approve(c, auth, run.id)
        assert done["state"] == "DONE", done.get("error")
        rec = _hook_record(done)
        assert rec["engine_id"] == "magnific-precision-v2" and rec["factor"] == 2 and rec["stub"] is False
        assert rec["provider_job_id"] == "mag-1" and rec["price_usd"] == MAGNIFIC_PRICE
        assert "fallback" not in rec
        sent = made[0].submitted[0]
        assert sent.inputs["image"]["role"] == "ai_layer" and sent.params["scale"] == 2
        assert _img(sent.inputs["image"]["bytes"]).shape[:2] == (180, 320)  # the take, not the plate
        _assert_passed(c.get(f"/api/v0/runs/{run.id}/gate", headers=auth).json())


def test_a_failing_magnific_falls_back_to_the_free_local_pass_and_keeps_the_take(
    sync_settings, auth, monkeypatch
):
    monkeypatch.setenv("PLATE_LIVE", "1")
    with _client(sync_settings) as c:
        up = layer_upscale.install(c.app.state.pipeline)
        up.magnific_factory = lambda run, manifest, route: FakeMagnific(manifest, route, fail=True)
        _setup(c, auth)
        run, _q = _quote(c, {"params": {"upscale_layers": "magnific"}})
        done = _approve(c, auth, run.id)
        assert done["state"] == "DONE", done.get("error")
        rec = _hook_record(done)
        assert rec["engine_id"] == "real-esrgan" and rec["stub"] is True and rec["factor"] == 2
        assert "the vendor gave up" in rec["fallback"]


def test_approval_rechecks_plate_live_for_a_declared_magnific_pass(sync_settings, auth, monkeypatch):
    monkeypatch.setenv("PLATE_LIVE", "1")
    with _client(sync_settings) as c:
        layer_upscale.install(c.app.state.pipeline)
        _setup(c, auth)
        run, _q = _quote(c, {"params": {"upscale_layers": "magnific"}})
        monkeypatch.delenv("PLATE_LIVE")
        r = c.post(f"/api/v0/runs/{run.id}/approve", json={"by": "human"}, headers=auth)
        assert r.status_code == 422 and r.json()["error"] == "magnific_live_only", r.text
        assert c.get(f"/api/v0/runs/{run.id}", headers=auth).json()["state"] == "QUOTED"  # nothing spent


def test_a_magnific_mode_without_a_declaration_never_calls_magnific(sync_settings, auth, monkeypatch):
    """A run whose params say magnific but whose quote did not declare it (quoted before the install): the free
    local pass runs and the record says why."""
    monkeypatch.setenv("PLATE_LIVE", "1")
    install = layer_upscale.install
    monkeypatch.setattr(
        layer_upscale, "install", lambda _pl: None
    )  # create_app installs it (T107): not for this run
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        _setup(c, auth)
        run, quote = _quote(c, {"params": {"upscale_layers": "magnific"}})  # before install: not declared
        assert quote.price_usd == pytest.approx(FAKE_PRICE)
        up = install(pl)
        up.magnific_factory = lambda *a: pytest.fail("Magnific must not be called for an undeclared pass")
        done = _approve(c, auth, run.id)
        assert done["state"] == "DONE", done.get("error")
        rec = _hook_record(done)
        assert rec["engine_id"] == "real-esrgan" and "did not pre-declare" in rec["fallback"]


# ---------------------------------------------------------------------------- repeats: journal and re-composite


def test_a_repeated_normalize_and_a_recomposite_reuse_the_upscaled_layer(sync_settings, auth, monkeypatch):
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        up = layer_upscale.install(pl)
        _setup(c, auth)
        run, _q = _quote(c, {})
        done = _approve(c, auth, run.id)
        assert done["state"] == "DONE", done.get("error")
        rec = _hook_record(done)
        cas = _cas(sync_settings)
        raw = cas.read_bytes(done["normalized"]["raw_key"])
        with c.app.state.index.project(PROJECT) as conn:
            parent = get_run(conn, run.id)
        staged, pv = pl.stage(parent, cas), pl.packet(PROJECT, parent.packet_id)
        monkeypatch.setattr(up, "local", lambda *a: pytest.fail("the layer must not be upscaled again"))

        # A daemon restarted inside NORMALIZING: the journal hands the same PNG back.
        again, again_rec = up.hook(parent, raw, staged, pv)
        assert again == cas.read_bytes(rec["output_key"]) and again_rec == rec

        # A re-composite (T99) of that take reuses the parent's PNG, whatever its own params say.
        child = parent.model_copy(
            update={
                "id": "run_child",
                "inputs": {**parent.inputs, "recomposite_of": parent.id},
                "params": {"recomposite_of": parent.id},
            }
        )
        out, child_rec = up.hook(child, raw, staged, pv)
        assert out == cas.read_bytes(rec["output_key"])
        assert child_rec["reused_from"] == parent.id and child_rec["engine_id"] == "real-esrgan"


def test_a_recomposite_of_an_off_parent_stays_off(sync_settings, auth):
    with _client(sync_settings) as c:
        pl = c.app.state.pipeline
        up = layer_upscale.install(pl)
        _setup(c, auth)
        run, _q = _quote(c, {"params": {"upscale_layers": "off"}})
        done = _approve(c, auth, run.id)
        cas = _cas(sync_settings)
        raw = cas.read_bytes(done["normalized"]["raw_key"])
        with c.app.state.index.project(PROJECT) as conn:
            parent = get_run(conn, run.id)
        child = parent.model_copy(
            update={
                "id": "run_child2",
                "inputs": {**parent.inputs, "recomposite_of": parent.id},
                "params": {},
            }
        )
        out, rec = up.hook(child, raw, pl.stage(parent, cas), pl.packet(PROJECT, parent.packet_id))
        assert out == raw and rec == {}
