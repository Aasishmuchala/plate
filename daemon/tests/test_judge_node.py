"""The Judge node through the daemon's own routes (router plan T66, decisions 38 and 41).

The upstream take is an Edit run on the daemon's echo FakeEngine (pass), or the same engine shifting the frame
twelve pixels (camera drift past the 8 px bound: GATE_FAIL). The judge talks to recorded fixtures only: a recorder
writes the recording the first time a request is seen (hand-authored replies in the Messages API shape) and a plain
`FixtureTransport` replays it afterwards, so no test reaches a network and the request is pinned by its key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from plate_daemon import motion_runs, nodes
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import FakeEngine
from plate_daemon.ledger import Ledger
from plate_daemon.llm import judge as judge_mod
from plate_daemon.llm import judge_node
from plate_daemon.llm.client import FixtureTransport
from plate_daemon.machine import get_run
from plate_daemon.projects import Project
from plate_daemon.sse import EventFilter

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
PROJECT = "plate-fixture"
MODEL = "claude-opus-5-5"

pytestmark = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file(), reason="fixtures/packets not built yet (T2)"
)


def _reply(score: float, verdict: str, issues: list[str], tell: str) -> dict[str, Any]:
    return {
        "text": json.dumps({"score": score, "verdict": verdict, "issues": issues, "strongest_tell": tell}),
        "usage": {"input_tokens": 2400, "output_tokens": 90},
        "stop_reason": "end_turn",
    }


SHIP = [
    _reply(9.1, "ship", ["Mullion rhythm matches the plate"], "none"),
    _reply(8.9, "ship", ["Slightly even paving tone"], "paving tone a touch uniform"),
]


class Recorder:
    """Records `responses` for any request it has no recording of, then answers from the recording."""

    name = "fixture"

    def __init__(self, root: Path, responses: list[dict[str, Any]]):
        self.inner = FixtureTransport(root)
        self.responses = responses
        self.recorded: list[str] = []

    def send(self, request):
        if not self.inner.path_for(request.key).is_file():
            self.inner.record(
                request, self.responses, note="hand-authored reply (T66 test); not a live capture"
            )
            self.recorded.append(request.key)
        return self.inner.send(request)


@dataclass
class Env:
    client: TestClient
    auth: dict[str, str]
    pipeline: Any
    runner: judge_node.JudgeRunner
    tmp: Path

    def project(self, *, nda: bool = False) -> None:
        r = self.client.post(
            "/api/v0/projects", json={"project_id": PROJECT, "custody": {"nda": nda}}, headers=self.auth
        )
        assert r.status_code == 201, r.text

    def packet(self) -> str:
        manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
        hdr = {**self.auth, "X-Plate-Packet-Version": manifest["packet_version"]}
        body = {"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}}
        r = self.client.post("/api/v0/packets", json=body, headers=hdr)
        assert r.status_code in (200, 202), r.text
        return r.json()["packet_id"]

    def graph(self, packet_id: str) -> None:
        body = {
            "nodes": [
                {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
                {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
                {"id": "j", "kind": "Judge", "label": "Judge"},
            ],
            "edges": [
                {"id": "p->e", "source": "p", "target": "e"},
                {"id": "e->j", "source": "e", "target": "j"},
            ],
        }
        r = self.client.put(f"/api/v0/projects/{PROJECT}/graph", json=body, headers=self.auth)
        assert r.status_code == 200, r.text

    def quote(self, node_id: str):
        return self.client.post(
            f"/api/v0/projects/{PROJECT}/nodes/{node_id}/quote", json={}, headers=self.auth
        )

    def approve(self, run_id: str, by: str = "human") -> dict[str, Any]:
        r = self.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": by}, headers=self.auth)
        assert r.status_code == 200, r.text
        r = self.client.get(f"/api/v0/runs/{run_id}", headers=self.auth)
        return r.json()

    def edit(self) -> dict[str, Any]:
        r = self.quote("e")
        assert r.status_code == 200, r.text
        return self.approve(r.json()["run"]["id"], by="auto")

    def node_runs(self, node_id: str) -> int:
        with self.pipeline.index.project(PROJECT) as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE node_id = ?", (node_id,)).fetchone()[0]

    def records(self) -> list[dict[str, Any]]:
        return Ledger(self.pipeline.project_dir(PROJECT)).records()


@pytest.fixture
def make_env(plate_env, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    settings = Settings.load()
    stack = []

    def make(shift_px: int = 0) -> Env:
        client = TestClient(create_app(settings, FakeEngine(echo=True, shift_px=shift_px)), base_url=BASE)
        client.__enter__()
        stack.append(client)
        pipeline = client.app.state.pipeline
        pipeline.sleep = lambda _s: None
        nodes.install(pipeline)
        motion_runs.install(pipeline)
        runner = judge_node.install(pipeline)
        return Env(client, {"Authorization": f"Bearer {settings.token}"}, pipeline, runner, tmp_path)

    yield make
    for client in stack:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------- wiring and refusals


def test_install_is_idempotent_and_leaves_other_kinds_alone(make_env):
    env = make_env()
    assert judge_node.install(env.pipeline) is env.runner and env.pipeline.judge_runner is env.runner
    env.project()
    env.graph(env.packet())
    edit = env.edit()  # an Edit still goes through the original pipeline under all three installs
    assert edit["state"] == "DONE", edit.get("error")


def test_judge_on_an_nda_project_is_422_unavailable_with_no_run(make_env):
    env = make_env()
    env.project(nda=True)
    env.graph(env.packet())
    assert env.edit()["state"] == "DONE"  # the fake engine is local C0: an NDA project may edit
    r = env.quote("j")
    assert r.status_code == 422, r.text
    assert r.json() == {"error": "unavailable", "detail": judge_mod.UNAVAILABLE_NOTICE}
    assert env.node_runs("j") == 0


def test_judge_with_nothing_to_judge_is_422(make_env):
    env = make_env()
    env.project()
    env.graph(env.packet())
    r = env.quote("j")
    assert r.status_code == 422 and r.json()["error"] == "no_candidate"
    assert env.node_runs("j") == 0


# ---------------------------------------------------------------------------- the quote


def test_the_quote_prices_two_opus_samples_on_the_llm_route(make_env):
    env = make_env()
    env.project()
    env.graph(env.packet())
    edit = env.edit()
    r = env.quote("j")
    assert r.status_code == 200, r.text
    q = r.json()
    quote, run = q["quote"], q["run"]
    assert (quote["provider"], quote["model"], quote["custody_grade"]) == ("anthropic", MODEL, "D")
    expected = judge_node.judge_estimate_usd(MODEL, 640, 360)
    assert quote["price_usd"] == pytest.approx(expected) and expected > 0
    assert expected == pytest.approx(2 * judge_node.judge_estimate_usd(MODEL, 640, 360, samples=1), abs=1e-6)
    assert run["state"] == "QUOTED" and run["route"]["engine_id"] == f"anthropic:{MODEL}"
    spec = run["inputs"]["judge"]
    assert spec["candidate_run"] == edit["id"] and spec["candidate"] == edit["outputs"][0]
    assert spec["nda"] is False and spec["crops"] == []  # a passing take has no flagged tiles


# ---------------------------------------------------------------------------- execution on recorded fixtures


def test_the_judge_ships_a_passing_take_on_recorded_fixtures(make_env):
    env = make_env()
    root = env.tmp / "llm-fixtures"
    recorder = Recorder(root, SHIP)
    env.runner.transport = recorder
    env.project()
    env.graph(env.packet())
    edit = env.edit()
    upstream = env.client.get(f"/api/v0/runs/{edit['id']}/gate", headers=env.auth).json()

    run = env.approve(env.quote("j").json()["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    verdict = run["normalized"]["judge"]
    assert verdict["verdict"] in ("ship", "fix") and verdict["verdict"] == "ship"
    assert verdict["samples"] == 2 and verdict["score"] == pytest.approx(9.0)
    assert run["outputs"] == []  # the judge makes no pixels: nothing to export through it
    report = run["gate_report"]
    assert report["verdict"] == upstream["verdict"] and report["judge"]["verdict"] == "ship"
    assert report["metrics"] == upstream["metrics"]  # the numeric report is the gates', untouched
    assert len(recorder.recorded) == 1 and (root / f"{recorder.recorded[0]}.json").is_file()

    recs = env.records()
    mine = [r["kind"] for r in recs if r.get("run_id") == run["id"]]
    assert mine == ["quoted", "approved", "submitted", "fetched", "llm", "done"]
    llm = next(r for r in recs if r.get("run_id") == run["id"] and r["kind"] == "llm")
    assert llm["data"]["verdict"] == "ship" and llm["data"]["samples"] == 2
    calls = [r for r in recs if (r.get("data") or {}).get("parent_run_id") == run["id"]]
    assert len(calls) == 2 and all(r["kind"] == "done" and r["data"]["llm"] is True for r in calls)
    assert all(r["engine_id"] == f"anthropic:{MODEL}" for r in calls)
    moves = [m["data"]["to"] for m in env.client.app.state.bus.read(EventFilter(PROJECT, run["id"]), 0)]
    assert [m for m in moves][-2:] == ["GATING", "DONE"]

    # a second take of the same request replays the recording with nothing recorded again
    env.runner.transport = FixtureTransport(root)
    again = env.approve(env.quote("j").json()["run"]["id"])
    assert again["state"] == "DONE" and again["normalized"]["judge"]["verdict"] == "ship"


def test_the_judge_says_fix_when_the_upstream_report_failed(make_env):
    env = make_env(shift_px=12)  # camera drift past the 8 px bound: the Edit fails its gates
    env.runner.transport = Recorder(env.tmp / "llm-fixtures", SHIP)
    env.project()
    env.graph(env.packet())
    edit = env.edit()
    assert edit["state"] == "GATE_FAIL", edit.get("error")

    q = env.quote("j").json()
    assert q["run"]["inputs"]["judge"]["upstream_verdict"] == "fail"
    crops = q["run"]["inputs"]["judge"]["crops"]
    assert crops and all(0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1 for b in crops)
    run = env.approve(q["run"]["id"])
    assert run["state"] == "DONE", run.get("error")
    verdict = run["normalized"]["judge"]
    assert verdict["verdict"] == "fix" and verdict["forced_by_gate"] is True
    assert run["gate_report"]["verdict"] == "fail"  # the judge never passes a gate failure


def test_a_judge_with_no_readable_verdict_fails_the_run(make_env):
    env = make_env()
    bad = [{"text": "Looks great to me!", "usage": {"input_tokens": 10, "output_tokens": 5}}]
    env.runner.transport = Recorder(env.tmp / "llm-fixtures", bad)
    env.project()
    env.graph(env.packet())
    env.edit()
    run = env.approve(env.quote("j").json()["run"]["id"])
    assert run["state"] == "FAILED" and run["reason_code"] == "normalize_error"
    assert "no verdict" in run["error"]
    with env.pipeline.index.project(PROJECT) as conn:
        assert get_run(conn, run["id"]).gate_report is None


def test_a_project_tightened_to_nda_after_the_quote_is_refused_at_approval(make_env):
    env = make_env()
    recorder = Recorder(env.tmp / "llm-fixtures", SHIP)
    env.runner.transport = recorder
    env.project()
    env.graph(env.packet())
    env.edit()
    run_id = env.quote("j").json()["run"]["id"]
    project = Project.load(env.pipeline.project_dir(PROJECT))
    with project.lock(timeout_s=5.0):
        project.doc["custody"] = {"nda": True}
        project.save()
    r = env.client.post(f"/api/v0/runs/{run_id}/approve", json={"by": "human"}, headers=env.auth)
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "custody" and recorder.recorded == []  # nothing reached the transport
    with env.pipeline.index.project(PROJECT) as conn:
        assert get_run(conn, run_id).state.value == "QUOTED"
