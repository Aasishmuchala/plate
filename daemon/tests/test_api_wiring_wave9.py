"""`api.create_app` wiring for Wave 9 (router plan T107).

What the composition root does on its own for the Wave 9 pieces, with no test calling an install: People in
motion, the free re-mask and the AI-layer upscale hook wrapped onto the Pipeline (in that order, after the Motion
runner); the route-health tracker; the nightly scheduler's `on_alert` on the event bus as `alert` records; the
carry-over of accepted layers when a superseding packet is ingested; and the plugin / alert figures in `/health`.
Every piece has its own suite; these tests only prove that `create_app` connects them.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import (
    canvas_rules,
    carryover,
    egress,
    layer_upscale,
    people_video,
    recomposite,
    route_health,
    scheduler,
)
from plate_daemon.api import RecentAlerts, alert_event, create_app, default_nightly
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.machine import get_run
from plate_daemon.pipeline import encode_png16
from plate_daemon.pixels_client import INPROC_ENV, PORT_ENV
from plate_daemon.projects import Project
from plate_daemon.routes import alerts as alert_routes
from plate_daemon.scheduler import JOB_ORDER, JobOutcome
from plate_daemon.sse import EventFilter

ROOT = Path(__file__).resolve().parents[2]
PACKETS = ROOT / "fixtures" / "packets"
ROOM_A, ROOM_A_V2 = (PACKETS / n for n in ("room_a", "room_a_v2"))
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # the fixture manifests' project_id
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

needs_packets = pytest.mark.skipif(
    not all((p / "manifest.json").is_file() and (p / "beauty.exr").is_file() for p in (ROOM_A, ROOM_A_V2)),
    reason="fixtures/packets not built yet (T2)",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _manifest(path: Path) -> dict[str, Any]:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent, so an Edit take is a real, gate-passable picture."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.sent[result_url[len(RESULT_SCHEME) :]]


class Clock:
    """One settable instant for the app's clock and the nightly's."""

    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class App:
    """A TestClient on `create_app(settings, **kwargs)` with the calls these tests make."""

    def __init__(self, client: TestClient, settings: Settings):
        self.client = client
        self.settings = settings
        self.auth = {"Authorization": f"Bearer {settings.token}"}

    @property
    def pipeline(self) -> Any:
        return self.client.app.state.pipeline

    def project(self, project_id: str = PROJECT, *, nda: bool = False) -> None:
        r = self.client.post(
            "/api/v0/projects", json={"project_id": project_id, "custody": {"nda": nda}}, headers=self.auth
        )
        assert r.status_code == 201, r.text

    def packet(self, path: Path) -> str:
        hdr = {**self.auth, "X-Plate-Packet-Version": _manifest(path)["packet_version"]}
        body = {"packet_path": str(path), "sender": {"name": "test", "version": "0"}}
        r = self.client.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code in (200, 202), r.text
        packet_id = r.json()["packet_id"]
        status = self.client.get(f"/api/v0/packets/{packet_id}", headers=self.auth).json()["status"]
        assert status == "ingested"
        return packet_id

    def put_graph(self, nodes_: list[dict[str, Any]], pairs: list[tuple[str, str]]):
        edges = [{"id": f"{a}->{b}", "source": a, "target": b} for a, b in pairs]
        return self.client.put(
            f"/api/v0/projects/{PROJECT}/graph", json={"nodes": nodes_, "edges": edges}, headers=self.auth
        )

    def save_graph(self, nodes_: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        """project.json as written by a server that derived the statuses (an approved still upstream)."""
        project = Project.load(self.pipeline.project_dir(PROJECT))
        with project.lock(timeout_s=5.0):
            project.save_graph({"nodes": nodes_, "edges": edges})

    def quote(self, node_id: str, body: dict[str, Any] | None = None):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/quote", json=body or {}, headers=self.auth
        )

    def plate_key(self, path: Path) -> str:
        from plate_pixels import imageio

        rgb = np.rint(imageio.read(path / "display.tif").rgb * 65535).astype(np.uint16)
        return self.pipeline.cas_for(PROJECT).put_bytes(encode_png16(rgb), "png")

    def events(self, kind: str | None = None, project_id: str = PROJECT) -> list[dict[str, Any]]:
        recs = self.client.app.state.bus.read(EventFilter(project_id), 0)
        return [r for r in recs if kind is None or r["kind"] == kind]

    def health(self) -> dict[str, Any]:
        r = self.client.get("/api/v0/health")  # no token: the plugin's handshake
        assert r.status_code == 200, r.text
        return r.json()

    def runs_of(self, packet_id: str) -> list[Any]:
        with self.pipeline.index.project(PROJECT) as conn:
            ids = [
                r[0]
                for r in conn.execute("SELECT id FROM runs WHERE packet_id = ? ORDER BY id", (packet_id,))
            ]
            return [get_run(conn, i) for i in ids]

    def accepted_edit_on(self, packet_id: str) -> str:
        """Plate -> Edit (fake engine) quoted, approved to DONE, scrubbed and accepted. Returns the run id."""
        r = self.put_graph(
            [
                {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
            ],
            [("p", "e")],
        )
        assert r.status_code == 200, r.text
        q = self.quote("e")
        assert q.status_code == 200, q.text
        run_id = q.json()["run"]["id"]
        a = self.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=self.auth)
        assert a.status_code == 200, a.text
        assert self.client.get(f"/api/v0/runs/{run_id}", headers=self.auth).json()["state"] == "DONE"
        s = self.client.post(
            f"/api/v0/runs/{run_id}/scrub", json={"quadrants_visited": 4, "zoom_100": True}, headers=self.auth
        )
        assert s.status_code == 200 and s.json()["full_scrub"] is True, s.text
        acc = self.client.post(
            f"/api/v0/runs/{run_id}/accept",
            json={"scrub_log_id": s.json()["scrub_log_id"]},
            headers=self.auth,
        )
        assert acc.status_code == 200, acc.text
        return run_id


class NightlyFactory:
    """A `nightly_factory` over the real `scheduler.Nightly` with a timer that never fires, every job a no-op except
    `prices_refresh` (two engine alerts) and the ones named in `fail`. It never passes `on_alert`: the app must."""

    def __init__(self, clock: Clock, *, fail: tuple[str, ...] = (), project_id: str | None = None):
        self.clock = clock
        self.fail = fail
        self.project_id = project_id
        self.built: list[scheduler.Nightly] = []

    @staticmethod
    def timer(delay: float, fn: Any) -> Any:
        t = SimpleNamespace(delay=delay, fn=fn, daemon=False)
        t.start = lambda: None
        t.cancel = lambda: None
        return t

    def jobs(self) -> dict[str, Any]:
        project_id = self.project_id

        def noop(ctx: Any) -> JobOutcome:
            return JobOutcome()

        def prices(ctx: Any) -> JobOutcome:
            ctx.alerts.write("price_change", "nano moved 12%", engine_id="nano-banana-2.1")
            ctx.alerts.write("requote", "r1 needs a new quote", severity="info")
            return JobOutcome()

        def named(ctx: Any) -> JobOutcome:
            ctx.alerts.nightly(
                "backup_failed", f"backup of {project_id} failed", data={"project_id": project_id}
            )
            return JobOutcome()

        def failing(ctx: Any) -> JobOutcome:
            raise RuntimeError("blew up")

        jobs: dict[str, Any] = {name: noop for name in JOB_ORDER}
        jobs["prices_refresh"] = prices
        if project_id:
            jobs["backup"] = named
        for name in self.fail:
            jobs[name] = failing
        return jobs

    def __call__(self, settings: Settings) -> scheduler.Nightly:
        n = scheduler.Nightly(settings, clock=self.clock, timer_factory=self.timer, env={}, jobs=self.jobs())
        self.built.append(n)
        return n


@pytest.fixture
def make_app(plate_env, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_NIGHTLY", "0")  # these tests opt in to a scheduler explicitly
    monkeypatch.delenv("PLATE_AUTO_CARRY_OVER", raising=False)
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    monkeypatch.delenv(INPROC_ENV, raising=False)
    monkeypatch.setenv(PORT_ENV, str(_free_port()))  # nothing listens: no pixels worker
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


# ---------------------------------------------------------------------------- the installs


def test_create_app_installs_the_wave9_pieces_and_every_install_is_idempotent(make_app):
    app = make_app()
    pl = app.pipeline
    runners = (pl.people_video, pl.recomposite_runner, pl.layer_upscaler)
    assert all(r is not None for r in runners)
    assert people_video.install(pl) is runners[0]
    assert recomposite.install(pl) is runners[1]
    assert layer_upscale.install(pl) is runners[2]
    # the ingress hook is registered once, however many times the install is asked for
    assert [name for name, _ in pl.ingress_hooks].count(layer_upscale.HOOK_NAME) == 1
    assert isinstance(pl.route_tracker, route_health.Tracker)
    assert pl.route_tracker.path == app.settings.home / "cache" / route_health.FILE_NAME


def test_a_second_create_app_installs_again_on_its_own_pipeline_and_shares_route_health(make_app):
    first = make_app()
    second = create_app(first.settings)  # built, not entered: no lifespan, no scheduler
    p1, p2 = first.pipeline, second.state.pipeline
    assert p2 is not p1
    for pl in (p1, p2):
        assert (
            pl.people_video is not None
            and pl.recomposite_runner is not None
            and pl.layer_upscaler is not None
        )
        assert [name for name, _ in pl.ingress_hooks].count(layer_upscale.HOOK_NAME) == 1
    assert p2.people_video is not p1.people_video  # an install belongs to its pipeline
    # the tracker is a file under the home: what one app learns about a route the other sees
    for _ in range(route_health.DEGRADE_AFTER):
        p1.route_tracker.note_failure("kling-o3-4k", "atlas", "provider_poll_error")
    assert "kling-o3-4k/atlas" in p2.degraded_routes()
    assert "kling-o3-4k/atlas" in p1.degraded_routes()


@needs_packets
def test_people_in_motion_is_consulted_before_the_motion_runners_people(make_app):
    """People+video wraps the Motion runner: an unapproved clip refuses with the clip lock, before any run row.
    With the installs the other way round the Motion runner would quote this People node as a still take."""
    app = make_app()
    app.project()
    packet_id = app.packet(ROOM_A)
    still = {
        "id": "e",
        "kind": "Colour",
        "label": "Colour",
        "outputHash": app.plate_key(ROOM_A),
        "gateStatus": "pass",
        "approved": True,
        "approvedBy": "human",
        "outputTier": "measured",
    }
    people = {
        "id": "n",
        "kind": "People",
        "label": "People",
        "params": {"video": True, "cast": "woman in beige linen", "behavior": "walks toward the entrance"},
    }
    nodes_ = [
        {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
        still,
        {"id": "m", "kind": "Motion", "label": "Motion", "params": {"duration": 2}},
        people,
    ]
    edges = [
        {"id": "p->e", "source": "p", "target": "e"},
        {"id": "e->m", "source": "e", "target": "m"},
        {"id": "m->n", "source": "m", "target": "n", "targetHandle": "clip"},
    ]
    app.save_graph(nodes_, edges)
    r = app.quote("n")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "VIDEO_LOCKED" and r.json()["clip"] == "m"


@needs_packets
def test_an_edit_quote_with_recomposite_of_is_answered_by_the_recomposer(make_app):
    app = make_app()
    app.project()
    packet_id = app.packet(ROOM_A)
    r = app.put_graph(
        [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        [("p", "e")],
    )
    assert r.status_code == 200, r.text
    r = app.quote("e", {"params": {"recomposite_of": "run_nope"}})
    assert r.status_code == 404, r.text  # a plain Edit quote would have ignored the parameter
    assert "run_nope" in r.text


# ---------------------------------------------------------------------------- the nightly's alerts on the bus


def test_a_nightly_alert_reaches_the_bus_as_an_alert_record(make_app, monkeypatch):
    monkeypatch.delenv("PLATE_NIGHTLY")
    clock = Clock()
    factory = NightlyFactory(clock, fail=("canaries",))
    app = make_app(nightly_factory=factory)
    app.project()
    [nightly] = factory.built
    assert nightly.on_alert is not None  # attached by the app; the factory never passed one
    nightly.run_now()

    recs = app.events("alert")
    assert [r["data"]["alert_kind"] for r in recs] == ["price_change", "requote", "job_error"]
    assert all(r["data"]["kind"] == "alert" and r["run_id"] is None for r in recs)
    price, requote, job_error = (r["data"] for r in recs)
    # the level mapping of GET /alerts: engine kinds are fixed, the others keep the severity they were written with
    assert (price["level"], price["source"], price["engine_id"]) == ("warning", "engine", "nano-banana-2.1")
    assert (requote["level"], requote["message"]) == ("warning", "r1 needs a new quote")  # written as info
    assert (job_error["level"], job_error["source"]) == ("critical", "nightly")
    assert price["at"].startswith("2026-10-18T12:00")
    # and GET /alerts, the durable copy, says the same
    listed = app.client.get("/api/v0/alerts", headers=app.auth).json()
    assert {a["kind"]: a["level"] for a in listed} == {
        "price_change": "warning",
        "requote": "warning",
        "job_error": "critical",
    }


def test_an_alert_line_that_names_a_project_goes_to_that_project_only(make_app, monkeypatch):
    monkeypatch.delenv("PLATE_NIGHTLY")
    factory = NightlyFactory(Clock(), project_id="alerts-b")
    app = make_app(nightly_factory=factory)
    app.project("alerts-a")
    app.project("alerts-b")
    factory.built[0].run_now()
    named = [r["data"]["message"] for r in app.events("alert", "alerts-b")]
    unnamed = [r["data"]["message"] for r in app.events("alert", "alerts-a")]
    assert "backup of alerts-b failed" in named
    assert "backup of alerts-b failed" not in unnamed
    # an app-wide line (no project named) reaches every project
    assert {"nano moved 12%", "r1 needs a new quote"} <= set(named) & set(unnamed)


def test_with_no_project_an_alert_is_only_in_the_file_and_nothing_fails(make_app, monkeypatch, caplog):
    monkeypatch.delenv("PLATE_NIGHTLY")
    factory = NightlyFactory(Clock())
    app = make_app(nightly_factory=factory)
    with caplog.at_level(logging.WARNING, logger="plate_daemon"):
        rec = factory.built[0].run_now()
    assert rec["jobs"]["prices_refresh"]["status"] == "alerted"
    assert len(app.client.get("/api/v0/alerts", headers=app.auth).json()) == 2
    assert not [r for r in caplog.records if "on_alert failed" in r.getMessage()]


def test_a_factory_that_wired_its_own_on_alert_keeps_it(make_app, monkeypatch):
    monkeypatch.delenv("PLATE_NIGHTLY")
    seen: list[str] = []

    def factory(settings: Settings) -> scheduler.Nightly:
        return scheduler.Nightly(
            settings,
            timer_factory=NightlyFactory.timer,
            env={},
            on_alert=lambda line: seen.append(line["kind"]),
        )

    app = make_app(nightly_factory=factory)
    app.project()
    app.client.app.state.nightly._alerts().write("price_change", "moved", engine_id="nano-banana-2.1")
    assert seen == ["price_change"] and app.events("alert") == []


def test_the_default_nightly_takes_the_bus_callback(settings):
    sink = lambda line: None  # noqa: E731
    assert default_nightly(settings, on_alert=sink).on_alert is sink
    assert default_nightly(settings).on_alert is None


def test_alert_event_maps_a_line_the_way_the_alerts_route_does():
    event = alert_event(
        {
            "kind": "canary_drift",
            "severity": "warn",
            "message": "m",
            "at": "2026-10-18T01:02:03Z",
            "engine_id": "e1",
        }
    )
    assert event == {
        "kind": "alert",
        "source": "engine",
        "level": "error",
        "message": "m",
        "at": "2026-10-18T01:02:03Z",
        "alert_kind": "canary_drift",
        "engine_id": "e1",
    }
    bare = alert_event({"kind": "reconcile_failed", "severity": "warn", "message": "x"})
    assert bare["level"] == "warn" and bare["source"] == "nightly" and "engine_id" not in bare
    assert bare["at"]  # stamped now when the line has none


def test_a_nightly_alert_reaches_get_events(live_server, monkeypatch):
    """The real stream: a client that follows `GET /events` sees the nightly's line as kind `alert`."""
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.delenv("PLATE_NIGHTLY", raising=False)
    monkeypatch.setenv(PORT_ENV, str(_free_port()))
    factory = NightlyFactory(Clock(), fail=("backup",))
    with live_server(nightly_factory=factory) as (s, url):
        hdr = {"Authorization": f"Bearer {s.token}"}
        r = httpx.post(
            f"{url}/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": False}}, headers=hdr
        )
        assert r.status_code == 201, r.text
        factory.built[0].run_now()
        seen: list[dict[str, Any]] = []
        q = f"{url}/api/v0/events?project_id={PROJECT}&after=0&token={s.token}"
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=20) as c, c.stream("GET", q) as stream:
            assert stream.status_code == 200
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    seen.append(json.loads(line[5:].strip()))
                    if sum(1 for x in seen if x["kind"] == "alert") >= 3:
                        break
                assert time.monotonic() < deadline, f"no alerts on the stream: {seen}"
    alerts = [x["data"] for x in seen if x["kind"] == "alert"]
    assert [a["alert_kind"] for a in alerts] == ["price_change", "requote", "job_error"]
    assert alerts[0]["level"] == "warning" and alerts[2]["message"] == "backup: RuntimeError: blew up"


# ---------------------------------------------------------------------------- /health


def test_health_reports_the_plugin_queue_and_recent_alerts(make_app, monkeypatch):
    monkeypatch.delenv("PLATE_NIGHTLY")
    clock = Clock(T0 + timedelta(hours=1))
    factory = NightlyFactory(Clock(T0), fail=("backup",))
    app = make_app(nightly_factory=factory)
    app.client.app.state.clock = clock
    app.project()

    health = app.health()
    assert health["ok"] is True and health["pixels"] == "unavailable"  # the old keys are still there
    assert health["plugin"] == {"last_poll_utc": None, "queued": 0}
    assert health["alerts_recent"] == 0

    job = app.pipeline.node_runner.queue.enqueue(
        action="scout", project_id=PROJECT, payload={}, packet_id="pkt_x", now=T0
    )
    assert app.health()["plugin"] == {"last_poll_utc": None, "queued": 1}
    jobs = app.client.get("/api/v0/plugin/jobs?since=0&claim=1", headers=app.auth).json()["jobs"]
    assert [j["id"] for j in jobs] == [job.id]
    plugin = app.health()["plugin"]
    assert plugin["queued"] == 0 and isinstance(plugin["last_poll_utc"], str)

    factory.built[0].run_now()  # three alert lines stamped T0
    assert app.health()["alerts_recent"] == 3
    clock.now = T0 + timedelta(hours=23)
    assert app.health()["alerts_recent"] == 3
    clock.now = T0 + timedelta(hours=25)  # past the 24 hour window
    assert app.health()["alerts_recent"] == 0


def test_health_never_fails_over_the_alert_files_or_the_queue(make_app, monkeypatch, caplog):
    app = make_app()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk gone")

    monkeypatch.setattr(alert_routes, "collect", boom)
    monkeypatch.setattr(app.pipeline.node_runner.queue, "counts", boom)
    with caplog.at_level(logging.WARNING, logger="plate_daemon"):
        health = app.health()
    assert health["alerts_recent"] is None
    assert health["plugin"] == {"last_poll_utc": None, "queued": None}
    assert health["ok"] is True


def test_recent_alerts_rereads_only_when_a_file_changes_or_the_minute_turns(make_app, monkeypatch):
    app = make_app()
    clock = Clock()
    recent = RecentAlerts(lambda: app.client.app.state.ctx, clock)
    reads: list[int] = []
    real = alert_routes.collect

    def counted(c: Any, since: Any = None) -> Any:
        reads.append(1)
        return real(c, since)

    monkeypatch.setattr(alert_routes, "collect", counted)
    assert recent.count() == 0 and recent.count() == 0
    assert len(reads) == 1
    clock.now = T0 + timedelta(minutes=2)
    assert recent.count() == 0 and len(reads) == 2
    path = app.settings.home / "alerts.jsonl"
    line = {"v": 1, "at": "2026-10-18T12:01:00Z", "kind": "price_change", "severity": "warn", "message": "m"}
    path.write_text(json.dumps(line) + "\n", encoding="utf-8", newline="\n")
    assert recent.count() == 1 and len(reads) == 3


# ---------------------------------------------------------------------------- carry-over on supersede


@needs_packets
def test_ingesting_the_superseding_packet_carries_the_accepted_layer(make_app):
    app = make_app(engine=EchoEngine())
    app.project()
    old = app.packet(ROOM_A)
    accepted = app.accepted_edit_on(old)
    new = app.packet(ROOM_A_V2)
    assert _manifest(ROOM_A_V2)["supersedes"] == old

    [event] = app.events("packet.carried")
    data = event["data"]
    assert event["run_id"] is None
    assert data["packet_id"] == new and data["supersedes"] == old
    assert (data["carried"], data["dropped"]) == (1, 0)
    assert data["results"][0]["run_id"] == accepted and data["results"][0]["status"] == "carried"
    [carried] = app.runs_of(new)  # a new take on the new packet; nobody accepted it for the artist
    assert carried.route.engine_id == "carry-over" and carried.inputs["carried_from"] == accepted
    # the revision came first, then the carry
    kinds = [r["kind"] for r in app.events()]
    assert kinds.index("packet.superseded") < kinds.index("packet.carried")


@needs_packets
def test_the_environment_switch_turns_the_ingest_carry_off(make_app, monkeypatch):
    app = make_app(engine=EchoEngine())
    app.project()
    old = app.packet(ROOM_A)
    app.accepted_edit_on(old)
    monkeypatch.setenv("PLATE_AUTO_CARRY_OVER", "0")
    new = app.packet(ROOM_A_V2)
    assert app.events("packet.carried") == [] and app.runs_of(new) == []
    # the manual button still works
    r = app.client.post(f"/api/v0/packets/{new}/carry-over", headers=app.auth)
    assert r.status_code == 200 and r.json()[0]["status"] == "carried"


@needs_packets
def test_the_project_setting_turns_the_ingest_carry_off(make_app):
    app = make_app(engine=EchoEngine())
    app.project()
    old = app.packet(ROOM_A)
    app.accepted_edit_on(old)
    r = app.client.put(
        f"/api/v0/projects/{PROJECT}/settings", json={"auto_carry_over": False}, headers=app.auth
    )
    assert r.status_code == 200, r.text
    new = app.packet(ROOM_A_V2)
    assert app.events("packet.carried") == [] and app.runs_of(new) == []
    assert app.events("packet.superseded")  # the revision itself is not optional


@needs_packets
def test_a_carry_over_that_raises_never_fails_the_ingest(make_app, monkeypatch, caplog):
    app = make_app(engine=EchoEngine())
    app.project()
    old = app.packet(ROOM_A)
    app.accepted_edit_on(old)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("share went away")

    monkeypatch.setattr(carryover, "on_supersede", boom)
    with caplog.at_level(logging.WARNING, logger="plate_daemon"):
        new = app.packet(ROOM_A_V2)  # asserts the packet is ingested
    assert app.events("packet.carried") == []
    assert any("carry-over" in r.getMessage() and "share went away" in r.getMessage() for r in caplog.records)
    assert new in {r["data"]["packet_id"] for r in app.events("packet.ingested")}


@needs_packets
def test_a_packet_that_supersedes_nothing_carries_nothing(make_app):
    app = make_app(engine=EchoEngine())
    app.project()
    app.packet(ROOM_A)
    assert app.events("packet.carried") == []


# ---------------------------------------------------------------------------- the egress label scrubber


def test_every_canvas_node_kind_is_a_generic_label_to_the_egress_scrubber():
    """A node called "Reference 2" or "Sky 3" names a kind, not the client's project: it must not become a term the
    request scrubber looks for (and so blocks on)."""
    assert {k.lower() for k in canvas_rules.NODE_KINDS} <= egress.NODE_KINDS
    for kind in ("Reference", "Frame", "Atmosphere", "Sky"):
        assert egress._generic_node_label(f"{kind} 2"), kind
        assert egress._generic_node_label(f"{kind.lower()}-12"), kind
