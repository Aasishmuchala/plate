"""Wave 12 stress fixes in the run and misc routes (router plan T126), each test the attacker's repro.

- Decision 71: only a request carrying `X-Plate-Client: ui` resets the unattended clock. `/timer` (both actions),
  `/runs/{id}/scrub`, `/verdict`, `/override` and `/accept` used to reset it for any token holder, so a
  header-less `POST /timer` lifted the unattended cap for the next auto approvals.
- An approve that a cancel overtakes before the queue answers the typed 409 `illegal_transition`, never a 500.
- `POST /envelopes` refuses `Infinity`, `1e400`, `NaN` and out-of-range limits with 422 before any write, and a
  project that does not exist with 404 before its index is opened (no `index/<id>.sqlite` is left behind).
- An override typed into an export body is ledgered (and noted) before `export.export` writes a byte; a ledger
  that refuses it (503 `ledger_unavailable`, fail-closed from T145) leaves no file, no note and no record.

Runs that need a take go through the real pipeline on `fixtures/packets/room_a` with an engine that echoes the
PNG it was sent (shifted 12 px for a GATE_FAIL on camera drift), as `test_routes_runs_wave8.py` does.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import types
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import budgets
from plate_daemon.api import create_app
from plate_daemon.config import Settings, index_db_path
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.export import recorded_override
from plate_daemon.ledger import Ledger
from plate_daemon.pipeline import PipelineError
from plate_daemon.routes import runs as runs_routes
from plate_daemon.sse import EventFilter

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"  # room_a's manifest project_id
UI = {"X-Plate-Client": "ui"}
SENTINEL = datetime(2020, 1, 1, tzinfo=UTC)  # "a human looked, long ago": any reset moves it

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


@pytest.fixture
def labels_file(plate_env, monkeypatch) -> Path:
    path = plate_env / "calib" / "labels.jsonl"
    monkeypatch.setenv("PLATE_LABELS", str(path))
    return path


def _client(settings: Settings, engine=None) -> TestClient:
    c = TestClient(create_app(settings, engine or FakeEngine()), base_url=BASE)
    c.app.state.pipeline.sleep = lambda _s: None
    return c


def _project(c: TestClient, auth: dict, project_id: str = PROJECT) -> None:
    r = c.post("/api/v0/projects", json={"project_id": project_id, "custody": {"nda": False}}, headers=auth)
    assert r.status_code == 201, r.text


def _setup(c: TestClient, auth: dict) -> dict:
    """Project + room_a ingested + a graph of one Plate feeding one Edit node `e`. The graph."""
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
    r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth)
    assert r.status_code == 200, r.text
    return graph


def _quote(c: TestClient, auth: dict) -> dict:
    r = c.post(f"/api/v0/projects/{PROJECT}/nodes/e/quote", json={}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _take(c: TestClient, auth: dict, state: str) -> str:
    """Quote, auto-approve and run one take of `e` to its end; its id. `state` is the end it must reach."""
    run_id = _quote(c, auth)["run"]["id"]
    r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
    assert r.status_code == 200, r.text
    run = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
    assert run["state"] == state, run.get("error")
    return run_id


def _full_scrub(c: TestClient, auth: dict, run_id: str) -> str:
    """A logged full scrub of the run, made without the UI header (so it does not move the clock)."""
    r = c.post(f"/api/v0/runs/{run_id}/scrub", json={"quadrants_visited": 4, "zoom_100": True}, headers=auth)
    assert r.status_code == 200 and r.json()["full_scrub"] is True, r.text
    return r.json()["scrub_log_id"]


def _records(settings: Settings, kind: str, run_id: str) -> list[dict]:
    led = Ledger(settings.projects_dir / PROJECT)
    assert led.verify() == (True, None)
    return [r for r in led.records() if r["kind"] == kind and r.get("run_id") == run_id]


def _export(c: TestClient, auth: dict, run_id: str, **body):
    return c.post(f"/api/v0/runs/{run_id}/export", json=body, headers=auth)


# ---------------------------------------------------------------------------- decision 71: the human clock


def _timer_start(c, headers, run_id, i):
    r = c.post("/api/v0/timer", json={"deliverable_id": f"shot-{i}", "action": "start"}, headers=headers)
    plain = {"Authorization": headers["Authorization"]}  # one timer runs at a time; this stop is not a human
    stop = c.post("/api/v0/timer", json={"deliverable_id": f"shot-{i}", "action": "stop"}, headers=plain)
    assert stop.status_code == 200, stop.text
    return r


def _timer_stop(c, headers, run_id, i):
    plain = {"Authorization": headers["Authorization"]}
    r = c.post("/api/v0/timer", json={"deliverable_id": f"shot-{i}", "action": "start"}, headers=plain)
    assert r.status_code == 200, r.text
    return c.post("/api/v0/timer", json={"deliverable_id": f"shot-{i}", "action": "stop"}, headers=headers)


def _scrub(c, headers, run_id, i):
    body = {"quadrants_visited": 4, "zoom_100": True}
    return c.post(f"/api/v0/runs/{run_id}/scrub", json=body, headers=headers)


def _verdict(c, headers, run_id, i):
    sid = _full_scrub(c, {"Authorization": headers["Authorization"]}, run_id)
    body = {"verdict": "ship", "reasons": [], "scrub_log_id": sid, "rater": "aasish"}
    return c.post(f"/api/v0/runs/{run_id}/verdict", json=body, headers=headers)


def _override(c, headers, run_id, i):
    body = {"reason": f"the client signed take {i} off", "by": "human"}  # `by: human` is not a human
    return c.post(f"/api/v0/runs/{run_id}/override", json=body, headers=headers)


def _accept(c, headers, run_id, i):
    sid = _full_scrub(c, {"Authorization": headers["Authorization"]}, run_id)
    return c.post(f"/api/v0/runs/{run_id}/accept", json={"scrub_log_id": sid}, headers=headers)


HUMAN_ROUTES = [
    pytest.param(_timer_start, None, id="timer-start"),
    pytest.param(_timer_stop, None, id="timer-stop"),
    pytest.param(_scrub, "DONE", id="scrub", marks=needs_room_a),
    pytest.param(_verdict, "DONE", id="verdict", marks=needs_room_a),
    pytest.param(_override, "GATE_FAIL", id="override", marks=needs_room_a),
    pytest.param(_accept, "DONE", id="accept", marks=needs_room_a),
]


@pytest.mark.parametrize(("call", "needs"), HUMAN_ROUTES)
def test_only_a_ui_request_resets_the_unattended_clock(call, needs, sync_settings, auth, labels_file):
    with _client(sync_settings, EchoEngine(shift_px=12 if needs == "GATE_FAIL" else 0)) as c:
        run_id = None
        if needs is not None:
            _setup(c, auth)
            run_id = _take(c, auth, needs)
        pipeline = c.app.state.pipeline
        pipeline.last_human_utc = SENTINEL
        for i, extra in enumerate(({}, {"X-Plate-Client": "mcp"}, {"X-Plate-Client": "cli"})):
            r = call(c, auth | extra, run_id, i)
            assert r.status_code == 200, r.text
            assert pipeline.last_human_utc == SENTINEL, extra  # curl, MCP, a terminal: not a human
        r = call(c, auth | UI, run_id, 9)
        assert r.status_code == 200, r.text
        assert pipeline.last_human_utc is not None and pipeline.last_human_utc > SENTINEL


@needs_room_a
def test_a_header_less_timer_no_longer_lifts_the_unattended_cap(sync_settings, auth):
    with _client(sync_settings, EchoEngine()) as c:
        graph = _setup(c, auth)
        pipeline = c.app.state.pipeline
        pipeline.policy = budgets.Policy(per_run_auto_cap_usd=0.5, unattended_cap_usd=0.12)
        assert pipeline.last_human_utc is None  # nobody at the UI yet: every auto approval is unattended

        def approve(run_id: str):
            body = {"by": "auto", "start": False}  # APPROVED reserves the spend; nothing needs to run
            return c.post(f"/api/v0/runs/{run_id}/approve", json=body, headers=auth)

        for _ in range(3):
            quoted = _quote(c, auth)
            assert quoted["quote"]["price_usd"] == pytest.approx(0.04)  # three fit 0.12, a fourth does not
            r = approve(quoted["run"]["id"])
            assert r.status_code == 200 and r.json()["state"] == "APPROVED", r.text
        fourth = _quote(c, auth)["run"]["id"]
        r = approve(fourth)
        assert r.status_code == 409 and r.json()["error"] == "unattended_cap", r.text

        # the attacker's lift: a header-less timer start (and stop) is not a human any more
        for action in ("start", "stop"):
            r = c.post("/api/v0/timer", json={"deliverable_id": "hero", "action": action}, headers=auth)
            assert r.status_code == 200, r.text
        assert pipeline.last_human_utc is None
        fifth = _quote(c, auth)["run"]["id"]
        r = approve(fifth)
        assert r.status_code == 409 and r.json()["error"] == "unattended_cap", r.text
        r = approve(fourth)
        assert r.status_code == 409 and r.json()["error"] == "unattended_cap", r.text

        # a person at the UI is
        r = c.put(f"/api/v0/projects/{PROJECT}/graph", json=graph, headers=auth | UI)
        assert r.status_code == 200, r.text
        assert pipeline.last_human_utc is not None
        r = approve(fifth)
        assert r.status_code == 200 and r.json()["state"] == "APPROVED", r.text


# ---------------------------------------------------------------------------- approve overtaken by a cancel


@needs_room_a
def test_a_cancel_between_approve_and_queue_is_a_typed_409(sync_settings, auth, monkeypatch):
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        run_id = _quote(c, auth)["run"]["id"]
        pipeline = c.app.state.pipeline
        real_queue = pipeline.queue

        def overtaken(conn, project_id, rid):
            pipeline.cancel(conn, project_id, rid, actor="human", reason="cancelled while the approval ran")
            return real_queue(conn, project_id, rid)

        monkeypatch.setattr(pipeline, "queue", overtaken)
        r = c.post(f"/api/v0/runs/{run_id}/approve", json={"by": "auto"}, headers=auth)
        assert r.status_code == 409, r.text
        body = r.json()
        assert (body["error"], body["from"], body["to"]) == ("illegal_transition", "CANCELLED", "QUEUED")
        # the approval already committed stays committed
        view = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert view["state"] == "CANCELLED" and view["approved_by"] == "auto"
        states = [e["to_state"] for e in c.get(f"/api/v0/runs/{run_id}/events", headers=auth).json()]
        assert "APPROVED" in states and states[-1] == "CANCELLED" and "QUEUED" not in states


# ---------------------------------------------------------------------------- envelope bounds and ghosts


def _envelope_raw(c: TestClient, auth: dict, text: str):
    return c.post(
        "/api/v0/envelopes", content=text.encode("utf-8"), headers=auth | {"Content-Type": "application/json"}
    )


def _envelope_text(total: str = "1", per_run: str = "1", ttl: str = "3600", owner: str = "agent:test") -> str:
    # raw JSON: httpx refuses to encode NaN or Infinity, and an attacker's curl does not
    return (
        f'{{"project_id": "{PROJECT}", "owner": "{owner}", "max_total_usd": {total}, '
        f'"max_per_run_usd": {per_run}, "ttl_s": {ttl}}}'
    )


def _envelope_rows(c: TestClient) -> int:
    with c.app.state.index.project(PROJECT) as conn:
        return conn.execute("SELECT COUNT(*) FROM budget_envelopes").fetchone()[0]


def test_envelope_limits_must_be_finite_and_bounded(sync_settings, auth):
    with _client(sync_settings) as c:
        _project(c, auth)
        for total, per_run, ttl in (
            ("Infinity", "1", "3600"),
            ("1e400", "1", "3600"),
            ("NaN", "1", "3600"),
            ("-Infinity", "1", "3600"),
            ("1", "Infinity", "3600"),
            ("1", "1e400", "3600"),
            ("1", "NaN", "3600"),
            ("1", "1", "1e30"),
            ("1", "1", "Infinity"),
            ("1", "1", "NaN"),
            ("1000000.01", "1", "3600"),
            ("1", "1000001", "3600"),
            ("1", "1", "31622401"),
            ("-1", "1", "3600"),
            ("1", "1", "0"),
        ):
            r = _envelope_raw(c, auth, _envelope_text(total, per_run, ttl))
            assert r.status_code == 422, (total, per_run, ttl, r.text)
        assert _envelope_rows(c) == 0
        # the bounds themselves are allowed
        r = _envelope_raw(c, auth, _envelope_text("1000000", "1000000", "31622400"))
        assert r.status_code == 201, r.text
        assert r.json()["max_total_usd"] == 1_000_000 and r.json()["project_id"] == PROJECT
        assert _envelope_rows(c) == 1


def test_an_envelope_for_a_ghost_project_is_404_and_opens_no_index(sync_settings, auth):
    with _client(sync_settings) as c:
        body = {
            "project_id": "never-created",
            "owner": "agent:test",
            "max_total_usd": 5,
            "max_per_run_usd": 1,
            "ttl_s": 3600,
        }
        r = c.post("/api/v0/envelopes", json=body, headers=auth)
        assert r.status_code == 404, r.text
        assert r.json() == {"error": "not_found", "project_id": "never-created"}
        assert not index_db_path("never-created", sync_settings.home).exists()
        r = c.post("/api/v0/envelopes", json={**body, "project_id": "Not A Slug"}, headers=auth)
        assert r.status_code == 422, r.text
        assert "never-created" not in c.app.state.index.project_ids()

        # a bad owner on a real project: 422, no envelope row
        _project(c, auth)
        for owner in ("robot", "human", "agent:", "user:"):
            r = c.post(
                "/api/v0/envelopes", json={**body, "project_id": PROJECT, "owner": owner}, headers=auth
            )
            assert r.status_code == 422, (owner, r.text)
        assert _envelope_rows(c) == 0
        r = c.post("/api/v0/envelopes", json={**body, "project_id": PROJECT}, headers=auth)
        assert r.status_code == 201, r.text
        assert _envelope_rows(c) == 1


# ---------------------------------------------------------------------------- override before bytes


@needs_room_a
def test_a_typed_override_is_on_the_record_before_export_writes_a_byte(
    sync_settings, auth, tmp_path, monkeypatch
):
    with _client(sync_settings, EchoEngine(shift_px=12)) as c:
        _setup(c, auth)
        run_id = _take(c, auth, "GATE_FAIL")
        typed = "the client signed the drifted frame off"
        out = tmp_path / "out"
        seen: dict = {}
        real_export = runs_routes.export

        def watched(run, fmt, out_dir, override=None, **kw):
            seen["note"] = recorded_override(kw["conn"], run.id)
            seen["ledgered"] = [r.get("reason") for r in _records(sync_settings, "override", run.id)]
            seen["files"] = sorted(p.name for p in out.iterdir()) if out.exists() else []
            return real_export(run, fmt, out_dir, override, **kw)

        monkeypatch.setattr(runs_routes, "export", watched)
        r = _export(c, auth, run_id, out_dir=str(out), format="png16", override=True, reason=typed)
        assert r.status_code == 200, r.text
        assert seen == {"note": typed, "ledgered": [typed], "files": []}
        (ovr,) = _records(sync_settings, "override", run_id)
        (exp,) = _records(sync_settings, "export", run_id)
        assert ovr["seq"] < exp["seq"] and ovr["by"] == "human" and ovr["reason"] == typed
        assert exp["data"]["override_reason"] == typed and "repeat" not in exp["data"]
        assert (out / f"{run_id}.png").is_file()
        events = c.app.state.bus.read(EventFilter(PROJECT, run_id), 0)
        assert [e["data"]["reason"] for e in events if e["kind"] == "run.override"] == [typed]

        # a reason too short to override with is refused before anything is recorded
        r = _export(c, auth, run_id, out_dir=str(tmp_path / "short"), override=True, reason="too short")
        assert r.status_code == 409 and r.json()["error"] == "not_exportable", r.text
        assert len(_records(sync_settings, "override", run_id)) == 1
        assert not (tmp_path / "short").exists()


@needs_room_a
def test_a_ledger_that_refuses_the_typed_override_leaves_nothing_behind(
    sync_settings, auth, tmp_path, monkeypatch
):
    with _client(sync_settings, EchoEngine(shift_px=12)) as c:
        _setup(c, auth)
        run_id = _take(c, auth, "GATE_FAIL")
        pipeline = c.app.state.pipeline
        real_ledger = pipeline.ledger

        def refusing(project_id, kind, run, **kw):
            if kind == "override":
                raise PipelineError(
                    "the project ledger is unavailable (read-only runs.jsonl)",
                    code="ledger_unavailable",
                    status=503,
                    run_id=run.id,
                )
            return real_ledger(project_id, kind, run, **kw)

        monkeypatch.setattr(pipeline, "ledger", refusing)
        out = tmp_path / "out"
        reason = "client approved the shifted crop"
        r = _export(c, auth, run_id, out_dir=str(out), format="png16", override=True, reason=reason)
        assert r.status_code == 503, r.text
        assert r.json()["error"] == "ledger_unavailable" and r.json()["run_id"] == run_id
        assert not out.exists() or not any(out.iterdir())
        assert _records(sync_settings, "override", run_id) == []
        assert _records(sync_settings, "export", run_id) == []
        # no unledgered note either: nothing lets a later plain export out
        view = c.get(f"/api/v0/runs/{run_id}", headers=auth).json()
        assert view["override"] is None and view["exportable"] is False
        r = _export(c, auth, run_id, out_dir=str(out), format="png16")
        assert r.status_code == 409 and r.json()["error"] == "not_exportable", r.text
        assert not out.exists() or not any(out.iterdir())


@needs_room_a
def test_a_byte_identical_repeat_still_ships_and_is_ledgered_as_a_repeat(
    sync_settings, auth, tmp_path, monkeypatch
):
    """The route's half of the repeat contract (T127 makes `export.export` report `repeat` for a byte-identical
    re-export): the report is still marked shipped and the `export` record carries `data.repeat: true`."""
    with _client(sync_settings, EchoEngine()) as c:
        _setup(c, auth)
        run_id = _take(c, auth, "DONE")
        out = tmp_path / "out"
        real_export = runs_routes.export
        kept: list = []

        def keeping(*args, **kw):
            done = real_export(*args, **kw)
            kept.append(done)
            return done

        monkeypatch.setattr(runs_routes, "export", keeping)
        r = _export(c, auth, run_id, out_dir=str(out), format="png16", name="take")
        assert r.status_code == 200 and "repeat" not in r.json(), r.text
        first = r.json()
        (rec,) = _records(sync_settings, "export", run_id)
        assert "repeat" not in rec["data"]

        (done,) = kept
        fields = {f.name: getattr(done, f.name) for f in dataclasses.fields(done)}

        def repeated(run, fmt, out_dir, override=None, **kw):  # what T127's export answers for the same bytes
            return types.SimpleNamespace(**{**fields, "repeat": True})

        monkeypatch.setattr(runs_routes, "export", repeated)
        r = _export(c, auth, run_id, out_dir=str(out), format="png16", name="take")
        assert r.status_code == 200, r.text
        assert r.json() == {**first, "repeat": True} and r.json()["shipped"] is True
        records = _records(sync_settings, "export", run_id)
        assert len(records) == 2 and records[-1]["data"]["repeat"] is True
        assert records[-1]["data"]["dest_name"] == "take.png"
        assert c.get(f"/api/v0/runs/{run_id}/gate", headers=auth).json()["shipped"] is True


# ---------------------------------------------------------------------------- export names


def test_an_export_name_is_matched_whole(sync_settings, auth, tmp_path):
    with _client(sync_settings) as c:
        for name in ("nlx\n", "take\n", "a\nb"):
            r = _export(c, auth, "r-absent", out_dir=str(tmp_path), name=name)
            assert r.status_code == 422 and r.json()["error"] == "bad_name", (name, r.text)
            r = _export(c, auth, "r-absent", dest=str(tmp_path / f"{name}.tif"))
            assert r.status_code == 422 and r.json()["error"] == "bad_name", (name, r.text)
        assert _export(c, auth, "r-absent", out_dir=str(tmp_path), name="take_01").status_code == 404
