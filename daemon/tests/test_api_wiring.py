"""`api.create_app` as the daemon's composition root (router plan T73).

What the app wires on its own, with no test calling an install: the numeric, motion and judge node installs
(decision 38) and the router preview, the node routes (`routes/numeric.py`), the pixels seams behind
`Pipeline(ocr=, depth_fn=)` and `/health`, the nightly scheduler in the lifespan, `packet.ingested` and the
revisions reaction to a superseding packet, the JSON 404 for unknown API paths, and the UI's headers.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient

from plate_daemon import motion_runs, revisions, scheduler
from plate_daemon.api import UI_CSP, DaemonPipeline, MotionRouterPreview, create_app
from plate_daemon.config import Settings
from plate_daemon.ledger import Ledger
from plate_daemon.nodes import NUMERIC_ENGINE_ID
from plate_daemon.nodes import runner as node_runner
from plate_daemon.pipeline import encode_png16
from plate_daemon.pixels_client import INPROC_ENV, PORT_ENV, PixelsClient
from plate_daemon.projects import Project
from plate_daemon.sse import EventFilter

ROOT = Path(__file__).resolve().parents[2]
PACKETS = ROOT / "fixtures" / "packets"
ROOM_A, ROOM_B, ROOM_A_V2 = (PACKETS / n for n in ("room_a", "room_b", "room_a_v2"))
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # the fixture manifests' project_id
CARD_KEYS = {"verdict", "mode", "D_px_median", "disocclusion_fraction", "orbit_deg", "explanation"}

needs_packets = pytest.mark.skipif(
    not all((p / "manifest.json").is_file() for p in (ROOM_A, ROOM_B, ROOM_A_V2)),
    reason="fixtures/packets not built yet (T2)",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _manifest(path: Path) -> dict[str, Any]:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


class App:
    """A TestClient on `create_app(settings, **kwargs)` with the calls these tests make."""

    def __init__(self, client: TestClient, settings: Settings):
        self.client = client
        self.settings = settings
        self.auth = {"Authorization": f"Bearer {settings.token}"}

    @property
    def pipeline(self) -> Any:
        return self.client.app.state.pipeline

    def project(self, *, nda: bool = False) -> None:
        r = self.client.post(
            "/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=self.auth
        )
        assert r.status_code == 201, r.text

    def packet(self, path: Path) -> str:
        hdr = {**self.auth, "X-Plate-Packet-Version": _manifest(path)["packet_version"]}
        body = {"packet_path": str(path), "sender": {"name": "test", "version": "0"}}
        r = self.client.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code in (200, 202), r.text
        packet_id = r.json()["packet_id"]
        assert (
            self.client.get(f"/api/v0/packets/{packet_id}", headers=self.auth).json()["status"] == "ingested"
        )
        return packet_id

    def put_graph(self, nodes_: list[dict[str, Any]], pairs: list[tuple[str, str]]):
        edges = [{"id": f"{a}->{b}", "source": a, "target": b} for a, b in pairs]
        return self.client.put(
            f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes_, "edges": edges}, headers=self.auth
        )

    def save_graph(self, nodes_: list[dict[str, Any]], pairs: list[tuple[str, str]]) -> None:
        """project.json as written by a server that derived the statuses (an approved still upstream)."""
        project = Project.load(self.pipeline.project_dir(PROJECT))
        edges = [{"id": f"{a}->{b}", "source": a, "target": b} for a, b in pairs]
        with project.lock(timeout_s=5.0):
            project.save_graph({"nodes": nodes_, "edges": edges})

    def graph(self) -> dict[str, Any]:
        r = self.client.get(f"/api/v0/projects/{PROJECT}/graph", headers=self.auth)
        assert r.status_code == 200, r.text
        body = r.json()
        return body.get("graph", body)

    def quote(self, node_id: str, body: dict[str, Any] | None = None):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/quote", json=body or {}, headers=self.auth
        )

    def preview(self, node_id: str, what: str, params: dict[str, Any] | None = None):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/preview",
            json={"what": what, "params": params or {}},
            headers=self.auth,
        )

    def plate_key(self, path: Path) -> str:
        from plate_pixels import imageio

        rgb = np.rint(imageio.read(path / "display.tif").rgb * 65535).astype(np.uint16)
        return self.pipeline.cas_for(PROJECT).put_bytes(encode_png16(rgb), "png")

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        recs = self.client.app.state.bus.read(EventFilter(PROJECT), 0)
        return [r for r in recs if kind is None or r["kind"] == kind]


class Nightlies:
    """A nightly factory over the real `scheduler.Nightly` with a timer that never fires."""

    def __init__(self) -> None:
        self.built: list[Any] = []
        self.timers: list[Any] = []

    def timer(self, delay: float, fn: Any) -> Any:
        t = SimpleNamespace(delay=delay, fn=fn, daemon=False, started=False, cancelled=False)
        t.start = lambda: setattr(t, "started", True)
        t.cancel = lambda: setattr(t, "cancelled", True)
        self.timers.append(t)
        return t

    def __call__(self, settings: Settings) -> Any:
        n = scheduler.Nightly(settings, timer_factory=self.timer, env={})
        self.built.append(n)
        return n


@pytest.fixture
def make_app(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_NIGHTLY", "0")  # these tests opt in to a scheduler explicitly
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    monkeypatch.delenv(INPROC_ENV, raising=False)
    monkeypatch.setenv(
        PORT_ENV, str(_free_port())
    )  # nothing listens: no pixels worker unless a test starts one
    stack: list[TestClient] = []

    def make(**kwargs: Any) -> App:
        settings = Settings.load()
        client = TestClient(create_app(settings, **kwargs), base_url=BASE)
        client.__enter__()
        stack.append(client)
        client.app.state.pipeline.sleep = lambda _s: None
        return App(client, settings)

    yield make
    for client in reversed(stack):
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------- unknown API paths


def test_an_unknown_api_path_is_a_json_404_for_every_method(make_app):
    app = make_app()
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        r = app.client.request(method, "/api/v0/nope", headers=app.auth)
        assert r.status_code == 404, (method, r.text)
        assert r.json() == {"error": "not_found", "detail": "no route /api/v0/nope"}
        assert r.headers["x-content-type-options"] == "nosniff"
    # A known path with a method its route does not take: still a JSON 404, never a 405.
    r = app.client.post("/api/v0/session", json={}, headers=app.auth)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert r.json()["detail"] == "no route /api/v0/session"
    # The token is checked before any routing, the refusal carries the header too.
    r = app.client.post("/api/v0/nope")
    assert r.status_code == 401 and r.headers["x-content-type-options"] == "nosniff"
    # Real routes are not shadowed by the catch-all.
    assert app.client.get("/api/v0/session", headers=app.auth).status_code == 200
    assert app.client.get("/api/v0/health").headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------------- node installs and node routes


def test_create_app_installs_every_node_kind_before_serving(make_app):
    app = make_app()
    pl = app.pipeline
    assert isinstance(pl, DaemonPipeline)
    runners = (pl.node_runner, pl.motion_runner, pl.judge_runner)
    assert all(r is not None for r in runners)
    # idempotent: the tests' own installs (and the routes' install-on-first-use) find the same runners
    from plate_daemon import nodes
    from plate_daemon.llm import judge_node

    assert nodes.install(pl) is runners[0] and motion_runs.install(pl) is runners[1]
    assert judge_node.install(pl) is runners[2]
    assert isinstance(node_runner.PREVIEWS["router"], MotionRouterPreview)


def test_the_plugin_job_routes_are_mounted(make_app):
    app = make_app()
    r = app.client.get("/api/v0/plugin/jobs", headers=app.auth)
    assert r.status_code == 200, r.text
    assert r.json() == {"jobs": [], "next": 0}
    assert app.client.get("/api/v0/plugin/jobs").status_code == 401
    assert app.client.get("/api/v0/plugin/jobs/pj_nope", headers=app.auth).status_code == 404


@needs_packets
def test_a_set_node_quotes_through_the_quote_route(make_app):
    app = make_app()
    app.project()
    a, b = app.packet(ROOM_A), app.packet(ROOM_B)
    r = app.put_graph(
        [
            {"id": "pa", "kind": "Plate", "packetId": a, "tier": "A", "label": "A"},
            {"id": "pb", "kind": "Plate", "packetId": b, "tier": "A", "label": "B"},
            {"id": "n", "kind": "Set", "label": "Set", "params": {}},
        ],
        [("pa", "n"), ("pb", "n")],
    )
    assert r.status_code == 200, r.text
    r = app.quote("n")
    assert r.status_code == 200, r.text
    run, quote = r.json()["run"], r.json()["quote"]
    assert run["state"] == "QUOTED" and run["route"]["engine_id"] == NUMERIC_ENGINE_ID
    assert run["packet_id"] == a  # anchored to the hero
    assert quote["price_usd"] == 0.0 and quote["custody_grade"] == "C0"


@needs_packets
def test_a_motion_node_is_quoted_by_the_motion_runner(make_app):
    app = make_app()
    app.project()
    packet_id = app.packet(ROOM_A)
    edit = {"id": "e", "kind": "Edit", "label": "Edit", "outputHash": app.plate_key(ROOM_A)}  # not approved
    plate = {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}
    app.save_graph([plate, edit, {"id": "m", "kind": "Motion", "params": {}}], [("p", "e"), ("e", "m")])
    r = app.quote("m")
    assert r.status_code == 409 and r.json()["error"] == "VIDEO_LOCKED", r.text


@needs_packets
def test_a_judge_node_on_an_nda_project_is_unavailable(make_app):
    app = make_app()
    app.project(nda=True)
    packet_id = app.packet(ROOM_A)
    r = app.put_graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
            {"id": "j", "kind": "Judge", "label": "Judge"},
        ],
        [("p", "e"), ("e", "j")],
    )
    assert r.status_code == 200, r.text
    r = app.quote("j")
    assert r.status_code == 422 and r.json()["error"] == "unavailable", r.text


@needs_packets
def test_the_router_preview_answers_the_motion_editors_card(make_app):
    app = make_app()
    app.project()
    packet_id = app.packet(ROOM_A)
    plate = {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"}
    edit = {
        "id": "e",
        "kind": "Edit",
        "label": "Edit",
        "outputHash": app.plate_key(ROOM_A),
        "gateStatus": "pass",
        "approved": True,
    }
    app.save_graph([plate, edit, {"id": "m", "kind": "Motion"}], [("p", "e"), ("e", "m")])
    r = app.preview("m", "router", {"move": "push_in", "duration": 5})
    assert r.status_code == 200, r.text
    card = r.json()
    assert CARD_KEYS <= set(card)
    assert card["mode"] == "advisory" and card["source"] == "still" and card["verdict"] == "ai_interpolate"
    assert isinstance(card["explanation"], str) and card["explanation"]
    # The router card is for camera moves only; the numeric previews still answer on their own nodes.
    app.save_graph(
        [plate, {"id": "s", "kind": "Set", "params": {}}, {"id": "m", "kind": "Motion"}],
        [("p", "s")],
    )
    r = app.preview("s", "router")
    assert r.status_code == 404 and r.json()["error"] == "unknown_preview", r.text
    assert app.preview("s", "nope").status_code == 404


# ---------------------------------------------------------------------------- the nightly scheduler


def test_the_lifespan_starts_the_nightly_once_and_stops_it(make_app, monkeypatch):
    monkeypatch.delenv("PLATE_NIGHTLY")
    nightlies = Nightlies()
    app = make_app(nightly_factory=nightlies)
    nightly = app.client.app.state.nightly
    assert nightlies.built == [nightly] and nightly.status()["started"] is True
    assert len(nightlies.timers) == 1 and nightlies.timers[0].started and nightlies.timers[0].daemon
    assert nightly.start() is False  # a double start is a no-op: no second timer
    assert len(nightlies.timers) == 1
    app.client.__exit__(None, None, None)
    assert nightlies.timers[0].cancelled and nightly.status()["started"] is False
    # entering the lifespan again reuses the scheduler
    app.client.__enter__()
    assert nightlies.built == [nightly] and nightly.status()["started"] is True


def test_plate_nightly_0_starts_no_scheduler(make_app):
    nightlies = Nightlies()
    app = make_app(nightly_factory=nightlies)  # make_app sets PLATE_NIGHTLY=0
    assert nightlies.built == [] and app.client.app.state.nightly is None
    assert app.client.get("/api/v0/health").status_code == 200


def test_a_scheduler_that_cannot_be_built_never_blocks_serving(make_app, monkeypatch, caplog):
    monkeypatch.delenv("PLATE_NIGHTLY")

    def broken(settings: Settings) -> Any:
        raise scheduler.NightlyConfigError("nightly.time: '25:00' is not a clock time")

    with caplog.at_level(logging.WARNING, logger="plate_daemon"):
        app = make_app(nightly_factory=broken)
    assert app.client.app.state.nightly is None
    assert app.client.get("/api/v0/health").json()["ok"] is True
    assert any("nightly scheduler not started" in r.getMessage() for r in caplog.records)


def test_the_default_scheduler_writes_ledgers_through_the_project_folder(make_app, monkeypatch, tmp_path):
    monkeypatch.delenv("PLATE_NIGHTLY")
    built: list[dict[str, Any]] = []

    class Spy:
        def __init__(self, settings: Settings, **kwargs: Any):
            built.append({"settings": settings, **kwargs})
            self.started = 0
            self.stopped = 0

        def start(self) -> bool:
            self.started += 1
            return True

        def stop(self, wait_s: float = 0.0) -> None:
            self.stopped += 1

    monkeypatch.setattr(scheduler, "Nightly", Spy)
    app = make_app()
    spy = app.client.app.state.nightly
    assert isinstance(spy, Spy) and spy.started == 1 and len(built) == 1
    assert built[0]["settings"] is app.client.app.state.settings
    ledger = built[0]["ledger_factory"](SimpleNamespace(root=tmp_path / "proj"))
    assert isinstance(ledger, Ledger) and Path(ledger.project_dir) == tmp_path / "proj"
    app.client.__exit__(None, None, None)
    assert spy.stopped == 1
    app.client.__enter__()


# ---------------------------------------------------------------------------- the pixels worker


class Worker:
    """The real pixels worker app (stub backends) on a loopback socket."""

    def __init__(self, token: str):
        from plate_pixels.backends import Backends
        from plate_pixels.server import create_app as create_worker

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_worker(token, Backends(), port=self.port), log_level="warning", lifespan="on"
            )
        )
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def __enter__(self) -> Worker:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), "the worker did not start"
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.sock.close()


def test_with_no_pixels_worker_ocr_finds_nothing_and_depth_is_none(make_app, monkeypatch, caplog):
    app = make_app()
    pl = app.pipeline
    probes: list[int] = []
    real = PixelsClient.available

    def counted(self: PixelsClient) -> bool:
        probes.append(1)
        return real(self)

    monkeypatch.setattr(PixelsClient, "available", counted)
    assert app.client.get("/api/v0/health").json()["pixels"] == "unavailable"
    assert pl.depth_fn is None  # the gate's "no depth backend configured" path
    rgb = np.zeros((8, 8, 3), np.uint8)
    with caplog.at_level(logging.WARNING, logger="plate_daemon.pixels"):
        assert pl.ocr.detect(rgb) == [] and pl.ocr.detect(rgb) == []
    warnings = [r for r in caplog.records if r.name == "plate_daemon.pixels"]
    assert len(warnings) == 1 and "python -m plate_pixels" in warnings[0].getMessage()
    assert len(probes) == 1  # one probe per TTL, not one per call
    # nothing was built on the way: the client exists, no files were written
    assert isinstance(app.client.app.state.pixels, PixelsClient)
    assert not (app.settings.home / "cache" / "pixels").exists() or not list(
        (app.settings.home / "cache" / "pixels").iterdir()
    )
    # an explicit depth function (a test's fake, an operator's override) wins over the seam
    pl.depth_fn = lambda img: np.ones(img.shape[:2], np.float32)
    assert pl.depth_fn(rgb).shape == (8, 8)
    pl.depth_fn = None
    assert pl.depth_fn is None


def test_in_process_pixels(make_app, monkeypatch):
    monkeypatch.setenv(INPROC_ENV, "1")
    app = make_app()
    assert app.client.get("/api/v0/health").json()["pixels"] == "inproc"
    fn = app.pipeline.depth_fn
    assert fn is not None and fn.name == "pixels-inproc-depth"
    z = fn(np.zeros((16, 24, 3), np.uint8))
    assert z.dtype == np.float32 and z.shape == (16, 24)
    assert app.pipeline.ocr.name == "pixels-inproc"
    assert app.pipeline.ocr.detect(np.zeros((8, 8, 3), np.uint8)) == []


def test_pixels_over_http_and_a_worker_that_refuses_our_token(make_app, monkeypatch, caplog):
    app = make_app()
    with Worker(app.settings.token) as worker:
        monkeypatch.setenv(PORT_ENV, str(worker.port))
        app.client.app.state.pixels = PixelsClient(app.settings)  # a new client: probed afresh
        assert app.client.get("/api/v0/health").json()["pixels"] == "http"
        fn = app.pipeline.depth_fn
        assert fn is not None and fn.name == "pixels-http-depth"
        assert fn(np.zeros((12, 20, 3), np.uint8)).shape == (12, 20)
        assert app.pipeline.ocr.detect(np.zeros((8, 8, 3), np.uint8)) == []
    with Worker("x" * 43) as stranger:  # listening, but not ours: as good as no worker
        monkeypatch.setenv(PORT_ENV, str(stranger.port))
        app.client.app.state.pixels = PixelsClient(app.settings)
        assert app.client.get("/api/v0/health").json()["pixels"] == "unavailable"
        assert app.pipeline.depth_fn is None
        with caplog.at_level(logging.WARNING, logger="plate_daemon.pixels"):
            assert app.pipeline.ocr.detect(np.zeros((8, 8, 3), np.uint8)) == []


# ---------------------------------------------------------------------------- packet events and revisions


def _sse_until(url: str, token: str, project_id: str, want: Any, timeout_s: float = 20.0) -> list[dict]:
    """Bus records from `GET /events?project_id=` until `want(records)` holds."""
    seen: list[dict[str, Any]] = []
    q = f"{url}/api/v0/events?project_id={project_id}&after=0&token={token}"
    deadline = time.monotonic() + timeout_s
    with httpx.Client(timeout=timeout_s) as c, c.stream("GET", q) as r:
        assert r.status_code == 200, r.read()
        assert r.headers["x-content-type-options"] == "nosniff"
        for line in r.iter_lines():
            if line.startswith("data:"):
                seen.append(json.loads(line[5:].strip()))
                if want(seen):
                    return seen
            assert time.monotonic() < deadline, f"no matching event in {seen}"
    raise AssertionError(f"stream ended before a matching event: {seen}")


@needs_packets
def test_ingesting_room_a_publishes_packet_ingested_on_the_event_stream(live_server, monkeypatch):
    monkeypatch.setenv("PLATE_NIGHTLY", "0")
    monkeypatch.setenv(PORT_ENV, str(_free_port()))
    manifest = _manifest(ROOM_A)
    with live_server() as (s, url):
        hdr = {"Authorization": f"Bearer {s.token}", "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
        r = httpx.post(f"{url}/api/v0/packets", json=body, headers=hdr, timeout=30)
        assert r.status_code in (200, 202), r.text
        packet_id = r.json()["packet_id"]
        recs = _sse_until(
            url, s.token, manifest["project_id"], lambda rs: any(x["kind"] == "packet.ingested" for x in rs)
        )
    rec = next(x for x in recs if x["kind"] == "packet.ingested")
    assert rec["run_id"] is None and rec["project_id"] == manifest["project_id"]
    assert rec["data"] == {
        "kind": "packet.ingested",
        "packet_id": packet_id,
        "project_id": manifest["project_id"],
        "shot_id": manifest["shot_id"],
        "tier": manifest["tier"],
        "packet_kind": manifest["kind"],
        "supersedes": None,
    }


@needs_packets
def test_a_superseding_packet_rebinds_the_plate_and_says_so(make_app):
    app = make_app()
    app.project()
    old = app.packet(ROOM_A)
    plate = {"id": "p", "kind": "Plate", "packetId": old, "tier": "A", "label": "Plate"}
    r = app.put_graph([plate, {"id": "n", "kind": "Set", "params": {}}], [("p", "n")])
    assert r.status_code == 200, r.text
    new = app.packet(ROOM_A_V2)
    assert _manifest(ROOM_A_V2)["supersedes"] == old

    ingested = {r["data"]["packet_id"]: r["data"] for r in app.events("packet.ingested")}
    assert ingested[old]["supersedes"] is None and ingested[new]["supersedes"] == old
    sup = app.events("packet.superseded")
    assert len(sup) == 1 and sup[0]["run_id"] is None
    assert sup[0]["data"]["packet_id"] == new and sup[0]["data"]["supersedes"] == old
    assert sup[0]["data"]["invalidated"] is True
    assert [n["node_id"] for n in sup[0]["data"]["rebound_nodes"]] == ["p"]
    # project.json (the truth) has the Plate on the new packet
    nodes_ = {n["id"]: n for n in app.graph()["nodes"]}
    assert nodes_["p"]["packetId"] == new


@needs_packets
def test_a_failed_revision_still_announces_the_supersede(make_app, monkeypatch, caplog):
    app = make_app()
    app.project()
    old = app.packet(ROOM_A)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(revisions, "on_new_packet", boom)
    with caplog.at_level(logging.WARNING, logger="plate_daemon"):
        new = app.packet(ROOM_A_V2)  # still ingested
    sup = app.events("packet.superseded")
    assert len(sup) == 1
    data = sup[0]["data"]
    assert data["packet_id"] == new and data["supersedes"] == old and data["invalidated"] is False
    assert data["reason"].startswith("revision failed: RuntimeError: index unavailable")
    assert any("revision" in r.getMessage() and "failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------- the built UI


def _dist(root: Path) -> Path:
    dist = root / "ui-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><title>PLATE</title><script type="module" src="/assets/index-abc.js"></script>\n',
        encoding="utf-8",
        newline="\n",
    )
    (dist / "assets" / "index-abc.js").write_text("console.log('plate')\n", encoding="utf-8", newline="\n")
    (dist / "assets" / "index-abc.css").write_text("body{margin:0}\n", encoding="utf-8", newline="\n")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>\n", encoding="utf-8")
    return dist


def test_the_ui_is_served_with_cache_and_content_security_headers(make_app, monkeypatch, tmp_path):
    monkeypatch.setenv("PLATE_UI_DIST", str(_dist(tmp_path)))
    app = make_app()
    c = app.client
    for path in ("/", "/index.html"):
        r = c.get(path)
        assert r.status_code == 200 and "<title>PLATE</title>" in r.text
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-security-policy"] == UI_CSP
    assert "media-src 'self' blob:" in UI_CSP  # the video player plays the clips it fetched as blobs
    etag = c.get("/").headers["etag"]
    r = c.get("/", headers={"If-None-Match": etag})
    assert r.status_code == 304 and r.headers["cache-control"] == "no-cache"
    for path in ("/assets/index-abc.js", "/assets/index-abc.css"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" not in r.headers
    r = c.get("/favicon.svg")  # not a hashed asset: no immutable
    assert r.status_code == 200 and "immutable" not in r.headers.get("cache-control", "")
    # the API keeps answering JSON under the mount, and an unknown API path is never the UI's HTML
    assert c.get("/api/v0/session", headers=app.auth).status_code == 200
    r = c.get("/api/v0/nope", headers=app.auth)
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert c.get("/", headers={"Host": "evil:4610"}).headers["x-content-type-options"] == "nosniff"


def test_the_not_built_page_carries_nosniff(make_app):
    r = make_app().client.get("/")
    assert r.status_code == 200 and r.text.startswith("UI not built")
    assert r.headers["x-content-type-options"] == "nosniff"
