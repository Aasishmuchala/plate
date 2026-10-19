"""Wave 13 pipeline (router plan T152; decisions 84, 85 and 86), each test the re-run of an attacker low.

- quote-cold-4k-over-2s: the render-it router's inputs (`detail_resolvability`) are computed once, at ingest, and
  stored in the index's `packet_router` table; a quote reads the row and decodes nothing for the router. A row of
  an older `ROUTER_SUMMARY_VERSION` is recomputed and replaced; a rebuild from the folder leaves the table empty
  (it is a cache) and the next quote refills it. Z and the object Cryptomatte come from one open of the EXR.
- fake-crash-not-exiting-in-background: a scripted `SimulatedCrash` exits a real `python -m plate_daemon` with
  code 70 on the executor's threads (`PLATE_RUN_WORKERS=4`) and in the inline BackgroundTask (`0`), not only on
  the dev step route; the restarted daemon adopts the provider's job by lookup (one submit, one job) and the
  ledger counts the money once.
- result-format-unrecorded: a JPEG, a WebP, a BMP, a 16-bit PNG and a fully transparent RGBA PNG each reach DONE
  with `normalized.ingress` naming the container, the bit depth, the channels and the alpha handling, and the
  gate report's `backend_notes` naming the lossy container and the alpha; a 32-bit float TIFF with a NaN inside
  the mask fails NORMALIZING (`normalize_error`, non-finite) instead of being cast into the composite.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon.api import create_app
from plate_daemon.cas import CAS
from plate_daemon.config import Settings
from plate_daemon.db import tx
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.pipeline import ROUTER_SUMMARY_VERSION, PacketView
from plate_pixels import exr

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
TERMINAL = ("DONE", "GATE_FAIL", "FAILED", "REFUSED", "CANCELLED", "EXPIRED")
QUOTE_BUDGET_S = 0.5  # decision 84: a quote with a stored router summary decodes no EXR for the router

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)


# ---------------------------------------------------------------------------- harness


def _client(settings: Settings, engine) -> TestClient:
    c = TestClient(create_app(settings, engine), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _manifest() -> dict:
    return json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))


def _graph(packet_id: str) -> dict:
    return {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }


def _wait(predicate, timeout_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    while True:
        got = predicate()
        if got:
            return got
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _setup(c: TestClient, auth: dict) -> str:
    """The project, room_a through `POST /packets` (polled to `ingested`) and a Plate -> Edit graph."""
    r = c.post("/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text
    hdr = {**auth, "X-Plate-Packet-Version": _manifest()["packet_version"]}
    r = c.post(
        "/api/v0/packets",
        json={"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}},
        headers=hdr,
    )
    assert r.status_code == 202, r.text
    packet_id = r.json()["packet_id"]
    _wait(
        lambda: c.get(f"/api/v0/packets/{packet_id}", headers=auth).json()["status"] == "ingested",
        30,
        "the packet to be ingested",
    )
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=_graph(packet_id), headers=auth)
    assert r.status_code == 200, r.text
    return packet_id


def _rows(c: TestClient) -> list[dict]:
    with c.app.state.index.project(PROJECT) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM packet_router ORDER BY packet_id")]


def _quote(c: TestClient, auth: dict) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={"engine_id": "fake"}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _cold(c: TestClient) -> None:
    """Forget every decoded packet, as a restarted daemon (or an evicted cache entry) would."""
    c.app.state.pipeline._packets.clear()


def _from_scratch(packet_id: str) -> dict:
    return PacketView.load(packet_id, ROOM_A).detail_resolvability()


def _same_numbers(router: dict, expected: dict) -> None:
    assert router["verdict"] == expected["verdict"]
    assert router["D_px_median"] == round(float(expected["D_px_median"]), 3)
    assert router["per_class"] == {k: round(float(v), 3) for k, v in expected["per_class"].items()}


class NoDecode:
    """Every EXR reader and the router math raise: a quote that still answers decoded nothing for the router.
    The quote's mask probe meets the same OSError and leaves the mask for staging (its documented path)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        from plate_gates import router_math

        self.calls: list[str] = []
        for module, name in (
            (exr, "read_elements"),
            (exr, "read_z_and_cryptomatte"),
            (router_math, "detail_resolvability"),
        ):
            monkeypatch.setattr(module, name, self._raiser(name))

    def _raiser(self, name: str):
        def raiser(*_a, **_k):
            self.calls.append(name)
            raise OSError(f"{name}: no EXR decode in this test")

        return raiser


# ---------------------------------------------------------------------------- (1)-(3) the router summary


def test_the_router_summary_is_stored_at_ingest_and_the_quote_reads_it(settings, auth, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="plate_daemon.pipeline")
    with _client(settings, FakeEngine(echo=True)) as c:
        packet_id = _setup(c, auth)
        rows = _rows(c)
        assert [(r["packet_id"], r["version"]) for r in rows] == [(packet_id, ROUTER_SUMMARY_VERSION)]
        assert rows[0]["computed_utc"].endswith("Z")
        assert any(f"router summary for {packet_id} computed in" in m for m in caplog.messages)
        expected = _from_scratch(packet_id)  # before the readers are taken away
        stored = json.loads(rows[0]["json"])
        assert stored["verdict"] == expected["verdict"]
        assert stored["D_px_median"] == pytest.approx(expected["D_px_median"])

        _quote(c, auth)  # imports and manifests warm, so the timing below measures the quote itself
        _cold(c)
        guard = NoDecode(monkeypatch)
        t0 = time.perf_counter()
        q = _quote(c, auth)
        elapsed = time.perf_counter() - t0
        _same_numbers(q["router"], expected)
        assert "detail_resolvability" not in guard.calls and "read_z_and_cryptomatte" not in guard.calls
        assert elapsed < QUOTE_BUDGET_S, f"quote took {elapsed:.3f} s"
        assert len(_rows(c)) == 1


def test_a_row_of_an_older_version_is_recomputed_and_replaced(settings, auth):
    with _client(settings, FakeEngine(echo=True)) as c:
        packet_id = _setup(c, auth)
        expected = _from_scratch(packet_id)
        bogus = {"verdict": "whole_frame_ok", "D_px_median": 999.0, "per_class": {}, "per_id": {}}
        with c.app.state.index.project(PROJECT) as conn, tx(conn):
            conn.execute(
                "UPDATE packet_router SET version = '2026.01.01', json = ?, computed_utc = '2026-01-01T00:00:00Z'",
                (json.dumps(bogus),),
            )
        _cold(c)
        q = _quote(c, auth)
        _same_numbers(q["router"], expected)  # the old row was not believed
        rows = _rows(c)
        assert [(r["packet_id"], r["version"]) for r in rows] == [(packet_id, ROUTER_SUMMARY_VERSION)]
        assert rows[0]["computed_utc"] != "2026-01-01T00:00:00Z"
        assert json.loads(rows[0]["json"])["verdict"] == expected["verdict"]

        # A current row that does not parse is recomputed too, never served.
        with c.app.state.index.project(PROJECT) as conn, tx(conn):
            conn.execute("UPDATE packet_router SET json = '{not json'")
        _cold(c)
        _same_numbers(_quote(c, auth)["router"], expected)
        assert json.loads(_rows(c)[0]["json"])["verdict"] == expected["verdict"]


def test_a_rebuild_leaves_the_table_empty_and_the_next_quote_refills_it(settings, auth):
    with _client(settings, FakeEngine(echo=True)) as c:
        packet_id = _setup(c, auth)
        assert len(_rows(c)) == 1
        out = c.app.state.index.rebuild_from_project(settings.projects_dir / PROJECT)
        assert out["problems"] == [] and out["packets"] == 1
        assert _rows(c) == []  # a cache, never folder truth
        _cold(c)
        _same_numbers(_quote(c, auth)["router"], _from_scratch(packet_id))
        assert [(r["packet_id"], r["version"]) for r in _rows(c)] == [(packet_id, ROUTER_SUMMARY_VERSION)]


def test_a_warm_up_that_fails_is_a_warning_and_the_quote_computes_lazily(settings, auth, monkeypatch, caplog):
    def broken(*_a, **_k):
        raise OSError("the EXR is on a NAS that went away")

    monkeypatch.setattr(exr, "read_z_and_cryptomatte", broken)
    with _client(settings, FakeEngine(echo=True)) as c:
        packet_id = _setup(c, auth)  # the packet is ingested regardless
        assert _rows(c) == []
        assert any(
            f"router summary for {packet_id} not computed at ingest" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )
        monkeypatch.undo()
        _cold(c)
        _same_numbers(_quote(c, auth)["router"], _from_scratch(packet_id))
        assert len(_rows(c)) == 1


def test_z_and_the_object_cryptomatte_come_from_one_open_of_the_exr(monkeypatch):
    manifest = _manifest()
    z, (ranks, cov) = exr.read_z_and_cryptomatte(ROOM_A / "beauty.exr", manifest, "object")
    want_ranks, want_cov = exr.read_cryptomatte(ROOM_A / "beauty.exr", manifest, "object")
    want_z = exr.read_elements(ROOM_A / "beauty.exr", manifest, ["zdepth"])["zdepth"]
    assert np.array_equal(z, want_z) and np.array_equal(ranks, want_ranks) and np.array_equal(cov, want_cov)

    expected = _from_scratch(manifest["packet_id"])
    opens: list[str] = []
    real = exr.OpenEXR.File

    def counting(*a, **k):
        opens.append(str(a[0]))
        return real(*a, **k)

    monkeypatch.setattr(exr.OpenEXR, "File", counting)
    pv = PacketView.load(manifest["packet_id"], ROOM_A)
    assert pv.detail_resolvability() == expected and len(opens) == 1
    assert pv.crypto("object") is not None and len(opens) == 1  # the Cryptomatte was kept from that open
    fresh = PacketView.load(manifest["packet_id"], ROOM_A)
    fresh.crypto("object")  # decoded already (a mask, the text lock): only Z is read for the router
    assert fresh.detail_resolvability() == expected and len(opens) == 3


# ---------------------------------------------------------------------------- (4) a scripted crash exits the process


class Daemon:
    """`python -m plate_daemon` on this test's home, its output in a file (never a pipe that can fill)."""

    def __init__(self, home: Path, port: int, token: str, child_env, fake: dict, workers: str):
        self.base = f"http://127.0.0.1:{port}/api/v0"
        self.token = token
        self.out = home / f"daemon-{uuid.uuid4().hex[:6]}.log"
        env = child_env(
            PLATE_HOME=str(home),
            PLATE_PORT=str(port),
            PLATE_FAKE_ENGINE=json.dumps(fake),
            PLATE_RUN_WORKERS=workers,
            PLATE_SYNC_INGEST="1",
            PLATE_NIGHTLY="0",
            PLATE_PIXELS_INPROC="1",
            PLATE_LOG_LEVEL="warning",
        )
        self.fh = open(self.out, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "plate_daemon"], stdout=self.fh, stderr=subprocess.STDOUT, env=env
        )
        deadline = time.monotonic() + 60
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(f"daemon exited {self.proc.returncode}: {self.log()[-2000:]}")
            try:
                if self.call("GET", "/session").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "daemon did not answer within 60 s"
            time.sleep(0.2)

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}", **kw.pop("headers", {})}
        return httpx.request(method, self.base + path, headers=headers, timeout=30, **kw)

    def log(self) -> str:
        self.fh.flush()
        return self.out.read_text(encoding="utf-8", errors="replace")

    def wait_exit(self, timeout_s: float) -> int | None:
        try:
            return self.proc.wait(timeout_s)  # reaps it: a zombie's pid would still look like a live lease
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(30)
        self.fh.close()


@pytest.mark.parametrize("workers", ["4", "0"], ids=["executor", "inline"])
def test_a_scripted_crash_exits_the_process_and_the_restart_spends_once(
    tmp_path, settings, child_env, free_port, workers
):
    port = free_port()
    crash = {"echo": True, "crash_after_submit_before_persist": True, "supports_lookup": True}
    d = Daemon(tmp_path, port, settings.token, child_env, crash, workers)
    try:
        r = d.call("POST", "/projects", json={"project_id": PROJECT, "custody": {"nda": False}})
        assert r.status_code == 201, r.text
        r = d.call(
            "POST",
            "/packets",
            json={"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}},
            headers={"X-Plate-Packet-Version": _manifest()["packet_version"]},
        )
        assert r.status_code == 202, r.text
        r = d.call("PUT", f"/projects/{PROJECT}/graph", json=_graph(r.json()["packet_id"]))
        assert r.status_code == 200, r.text
        r = d.call("POST", f"/projects/{PROJECT}/nodes/e/quote", json={"engine_id": "fake"})
        assert r.status_code == 200, r.text
        run_id, price = r.json()["run"]["id"], float(r.json()["quote"]["price_usd"])
        try:
            r = d.call("POST", f"/runs/{run_id}/approve", json={"by": "human"})
            assert r.status_code == 200, r.text
        except httpx.TransportError:  # the executor's thread may end the process before the answer is out
            pass
        code = d.wait_exit(15)
        assert code == 70, f"the daemon did not exit as a crashed process would ({code}): {d.log()[-1500:]}"
        assert "exiting as a crashed process would" in d.log()
    finally:
        d.stop()
    journal = json.loads((tmp_path / "fake_engine.json").read_text(encoding="utf-8"))
    assert journal["submit_count"] == 1 and len(journal["jobs"]) == 1  # the provider took the job, once

    d = Daemon(tmp_path, port, settings.token, child_env, {"echo": True, "supports_lookup": True}, workers)
    try:
        run = _wait(
            lambda: (x := d.call("GET", f"/runs/{run_id}").json()).get("state") in TERMINAL and x,
            90,
            "the restarted daemon to finish the run",
        )
        moves = [
            (e.get("from_state"), e.get("to_state"))
            for e in d.call("GET", f"/runs/{run_id}/events").json()
            if e.get("kind") == "transition"
        ]
        spend = d.call("GET", "/spend", params={"all_projects": "1"}).json()
        project_spend = d.call("GET", f"/projects/{PROJECT}/spend").json()
    finally:
        d.stop()
    journal = json.loads((tmp_path / "fake_engine.json").read_text(encoding="utf-8"))
    assert journal["submit_count"] == 1 and len(journal["jobs"]) == 1, journal  # never resubmitted
    if run["state"] == "DONE":
        assert ("STAGING", "SUBMITTED") in moves and moves[-1][1] == "DONE", moves
        assert run["provider_job_id"] in journal["jobs"]  # the job it lost, adopted
    else:
        assert (run["state"], run.get("reason_code")) == ("FAILED", "submit_state_unknown"), run
    for money in (spend, project_spend):
        assert money["generations"] == 1, money
        assert float(money["spent_usd_quoted"]) == pytest.approx(price), money  # counted once


# ---------------------------------------------------------------------------- (5) the result's format


class Recodes(FakeEngine):
    """An echoing fake whose result download is what egress sent, re-encoded as `fmt` (`nan_at`: a (y, x) of the
    sent image set to NaN in the float TIFF)."""

    def __init__(self, fmt: str, nan_at: tuple[int, int] | None = None):
        super().__init__(echo=True, polls_until_done=1)
        self.fmt, self.nan_at = fmt, nan_at

    def fetch(self, result_url):
        bgr = cv2.imdecode(np.frombuffer(super().fetch(result_url), np.uint8), cv2.IMREAD_UNCHANGED)
        if self.fmt == "jpeg":
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 98])
        elif self.fmt == "webp":
            ok, buf = cv2.imencode(".webp", bgr)  # OpenCV's default WebP is lossless
        elif self.fmt == "bmp":
            ok, buf = cv2.imencode(".bmp", bgr)
        elif self.fmt == "png16":
            ok, buf = cv2.imencode(".png", bgr.astype(np.uint16) * np.uint16(257))
        elif self.fmt == "rgba_clear":
            ok, buf = cv2.imencode(".png", np.dstack([bgr, np.zeros(bgr.shape[:2], np.uint8)]))
        elif self.fmt == "tiff32_nan":
            f = bgr.astype(np.float32) / 255.0
            assert self.nan_at is not None
            f[self.nan_at] = np.nan
            ok, buf = cv2.imencode(".tiff", f)
        else:  # pragma: no cover
            raise AssertionError(self.fmt)
        assert ok
        return bytes(buf)


FORMATS = [
    # fmt, raw_format, raw_bit_depth, lossy, alpha_dropped, alpha_transparent, the notes the gate report carries
    ("jpeg", "jpeg", 8, True, False, False, ["result container jpeg: lossy 8-bit"]),
    ("webp", "webp", 8, False, False, False, []),
    ("bmp", "bmp", 8, False, False, False, []),
    ("png16", "png", 16, False, False, False, []),
    ("rgba_clear", "png", 8, False, True, True, ["result alpha dropped", "result alpha fully transparent"]),
]
FORMAT_NOTES = ("result container", "result alpha")


@pytest.mark.parametrize(
    ("fmt", "raw_format", "depth", "lossy", "dropped", "clear", "notes"), FORMATS, ids=[f[0] for f in FORMATS]
)
def test_a_result_reaches_done_with_its_format_recorded(
    settings, auth, fmt, raw_format, depth, lossy, dropped, clear, notes
):
    with _client(settings, Recodes(fmt)) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        assert c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth).status_code == 200
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["state"] == "DONE", (run["state"], run.get("reason_code"), run.get("error"))
        ingress = run["normalized"]["ingress"]
        assert {k: ingress[k] for k in ("raw_format", "raw_bit_depth", "lossy")} == {
            "raw_format": raw_format,
            "raw_bit_depth": depth,
            "lossy": lossy,
        }
        assert ingress["raw_channels"] == (4 if dropped else 3)
        assert (ingress["alpha_dropped"], ingress["alpha_transparent"]) == (dropped, clear)
        rep = c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()
        assert [n for n in rep["backend_notes"] if n.startswith(FORMAT_NOTES)] == notes


def test_a_float_tiff_with_a_nan_inside_the_mask_fails_normalizing(settings, auth):
    engine = Recodes("tiff32_nan")
    with _client(settings, engine) as c:
        packet_id = _setup(c, auth)
        q = _quote(c, auth)
        pipeline = c.app.state.pipeline
        pv = pipeline.packet(PROJECT, packet_id)
        cas = CAS(settings.projects_dir / PROJECT / "assets")
        mask = pipeline.editable_mask_from_spec(q["run"]["inputs"]["mask"], pv, cas)
        ys, xs = np.nonzero(mask)
        assert ys.size, "room_a's Edit has an editable mask"
        engine.nan_at = (int(ys[ys.size // 2]), int(xs[ys.size // 2]))  # the fake's size map is the identity
        run_id = q["run"]["id"]
        assert c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=auth).status_code == 200
        run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert run["normalized"] is None or "outputs" not in (run["normalized"] or {})
    assert (run["state"], run["reason_code"]) == ("FAILED", "normalize_error"), run
    assert "non-finite" in run["error"] and "1 non-finite pixels of 230400" in run["error"], run["error"]
