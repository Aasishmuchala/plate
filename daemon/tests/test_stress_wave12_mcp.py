"""Wave 12 stress fixes on the MCP server (router plan T146, decision 78).

Each test encodes a repro from the mcp-cli-imports attacker, confirmed by a skeptic on a clean daemon:

- the NDA scrub was a denylist: `get_graph` on an NDA project handed over `negative_prompt`, override reasons and
  `by`, `outDir`, names, material names, change lists, instructions and tokens verbatim. It is an allowlist now;
- an NDA error message could quote the project (a rule message naming a node label, an unknown recipe path);
- `export_review` built a client bundle for a GATE_FAIL take with no override while `POST /review` and the CLI
  refused it. It asks the daemon's `GET /runs/{id}/reviewable` first;
- six `run_node` calls at once on one node all reported success and five were cancelled as duplicates (the MCP
  numbered the attempt from a ledger read); the daemon numbers it under its node lock now;
- twelve `add_node` calls at once reported duplicate ids and lost nodes (a read-modify-write PUT); they go
  through the append endpoint now;
- with `outbound.jsonl` unwritable, `run_node` had started and spent while the agent was told only
  `outbound_log_failed`: the error now names the run (or node) the daemon produced.

The tools run as `PlateTools(PlateBackend(TestClient))` against the daemon's own app on the committed packet
room_a, with a FakeEngine that echoes the PNG it was sent (optionally shifted 12 px, which fails the gates on
camera drift). `PLATE_RUN_WORKERS=0` keeps execution inline, so an approved run is DONE when the call returns.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from plate_daemon import mcp_server, outbound_log
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.mcp_server import McpError, PlateBackend, PlateTools
from plate_daemon.outbound_log import OutboundLog

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
API = "/api/v0"
PROJECT = "plate-fixture"  # room_a's manifest project_id
OPEN = "open-twin"  # the same graph on a project without NDA
REASON = "client signed off the drift on the east facade"
WITHHELD = "detail withheld on an NDA project"

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


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_RUN_WORKERS", "0")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


def _daemon(settings: Settings, auth: dict, engine: FakeEngine | None = None) -> TestClient:
    c = TestClient(create_app(settings, engine or EchoEngine()), base_url=BASE)
    c.headers.update(auth)
    return c


@pytest.fixture
def daemon(sync_settings, auth) -> Iterator[TestClient]:
    with _daemon(sync_settings, auth) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        yield c


def _tools(c: TestClient) -> PlateTools:
    return PlateTools(PlateBackend(c))


def _project(c: TestClient, project_id: str = PROJECT, *, nda: bool = False) -> None:
    r = c.post(f"{API}/projects", json={"project_id": project_id, "custody": {"nda": nda}})
    assert r.status_code == 201, r.text


def _packet(c: TestClient) -> str:
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    r = c.post(
        f"{API}/packets",
        json={"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}},
        headers={"X-Plate-Packet-Version": manifest["packet_version"]},
    )
    assert r.status_code == 202, r.text
    return r.json()["packet_id"]


def _put(c: TestClient, graph: dict, project_id: str = PROJECT) -> None:
    r = c.put(f"{API}/projects/{project_id}/graph", json=graph)
    assert r.status_code == 200, r.text


def _edit_graph(packet_id: str, **edit_params: Any) -> dict:
    return {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake", **edit_params}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }


def _ready(c: TestClient, *, nda: bool = False) -> str:
    """The project with room_a ingested and Plate -> Edit `e` on the fake engine. The packet id."""
    _project(c, nda=nda)
    packet_id = _packet(c)
    _put(c, _edit_graph(packet_id))
    return packet_id


def _envelope(c: TestClient, *, total: float = 10.0, project_id: str = PROJECT) -> str:
    body = {"project_id": project_id, "owner": "agent:test", "max_total_usd": total}
    r = c.post(f"{API}/envelopes", json={**body, "max_per_run_usd": total, "ttl_s": 3600})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _runs(c: TestClient, project_id: str = PROJECT) -> list[dict]:
    with c.app.state.index.project(project_id) as conn:
        rows = conn.execute("SELECT id, node_id, state, attempt, reason_code FROM runs ORDER BY attempt")
        return [dict(zip(("id", "node_id", "state", "attempt", "reason_code"), r, strict=True)) for r in rows]


def _zips(settings: Settings, project_id: str = PROJECT) -> list[Path]:
    review = settings.projects_dir / project_id / "review"
    return sorted(review.glob("*.zip")) if review.is_dir() else []


def _strings(value: Any, where: str = "") -> Iterator[tuple[str, str]]:
    """Every string in `value` (dict keys included), with its path written the way `scrub_nda` writes one."""
    if isinstance(value, dict):
        for k, v in value.items():
            sub = f"{where}.{k}" if where else str(k)
            yield sub, str(k)
            yield from _strings(v, sub)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{where}[{i}]")
    elif isinstance(value, str):
        yield where, value


# ---------------------------------------------------------------------------- the NDA scrub is an allowlist

FREE_TEXT = (
    "label",
    "prompt",
    "negative_prompt",
    "name",
    "materialName",
    "changelist",
    "instruction",
    "reason",
    "by",
    "outDir",
    "tokens",
    "recipe",
)


def _free_text_graph(packet_id: str) -> dict:
    """Every free-text field a person or a scene fills, each holding `ACME-<key>`."""

    def a(key: str) -> str:
        return f"ACME-{key}"

    return {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": packet_id, "tier": "A", "label": a("label")},
            {
                "id": "e",
                "kind": "Edit",
                "label": a("label"),
                "name": a("name"),
                "params": {
                    "engine": "fake",
                    "move": "push_in",  # an enum-valued param: passes even on an NDA project
                    "prompt": a("prompt"),
                    "negative_prompt": a("negative_prompt"),
                    "instruction": a("instruction"),
                    "changelist": a("changelist"),
                    "recipe": a("recipe"),
                    "tokens": {"mood": a("tokens"), "client": a("tokens")},
                },
                "overrides": [{"reason": a("reason"), "by": a("by"), "at": "2026-10-18T10:00:00Z"}],
            },
            {
                "id": "rc",
                "kind": "Recolour",
                "label": a("label"),
                "params": {
                    "materialName": a("materialName"),
                    "materials": [{"materialName": a("materialName")}],
                },
            },
            {
                "id": "x",
                "kind": "Export",
                "label": a("label"),
                "params": {"outDir": a("outDir"), "name": a("name")},
            },
        ],
        "edges": [
            {"id": "p->e", "source": "p", "target": "e"},
            {"id": "p->rc", "source": "p", "target": "rc"},
            {"id": "e->x", "source": "e", "target": "x"},
        ],
    }


@needs_room_a
def test_an_nda_graph_full_of_free_text_hands_over_none_of_it_and_names_every_path(daemon):
    c = daemon
    tools = _tools(c)
    _project(c, nda=True)
    _project(c, OPEN, nda=False)
    packet_id = _packet(c)
    graph = _free_text_graph(packet_id)
    _put(c, graph)
    _put(c, graph, OPEN)

    res = tools.get_graph(PROJECT)
    assert res["nda"] is True
    leaks = [(path, s) for path, s in _strings(res) if "ACME" in s]
    assert leaks == [], leaks
    stored = c.get(f"{API}/projects/{PROJECT}/graph").json()
    expected = {f"graph.{path}" for path, s in _strings(stored) if "ACME" in s}
    assert {f"graph.{p}" for p in ("nodes[1].overrides[0].reason", "nodes[1].overrides[0].by")} <= expected
    assert expected <= set(res["withheld"]), sorted(expected - set(res["withheld"]))
    assert "graph.nodes[1].overrides[0].at" in res["withheld"]  # a timestamp is a string like any other
    assert all(not p.startswith(("graph.edges", "graph.revision")) for p in res["withheld"])

    nodes = res["data"]["graph"]["nodes"]  # the structure is all there
    assert [(n["id"], n["kind"]) for n in nodes] == [
        ("p", "Plate"),
        ("e", "Edit"),
        ("rc", "Recolour"),
        ("x", "Export"),
    ]
    assert nodes[0]["packetId"] == packet_id and nodes[0]["tier"] == "A"
    assert nodes[1]["params"]["engine"] == "fake" and nodes[1]["params"]["move"] == "push_in"
    assert res["data"]["graph"]["edges"] == stored["edges"] and res["data"]["graph"]["revision"] == 1

    clear = json.dumps(tools.get_graph(OPEN))  # a project without NDA gets every word back
    assert "withheld" not in clear
    for key in FREE_TEXT:
        assert f"ACME-{key}" in clear, key


@needs_room_a
def test_list_projects_scrubs_only_the_nda_projects(daemon):
    c = daemon
    _project(c, nda=True)
    _project(c, OPEN, nda=False)
    res = _tools(c).list_projects()
    by_id = {p["project_id"]: p for p in res["data"]["projects"]}
    assert res["nda"] is True and by_id[PROJECT]["nda"] is True and by_id[OPEN]["nda"] is False
    assert by_id[OPEN]["created_utc"] and by_id[OPEN]["custody"]["allowed_grades"]
    assert by_id[PROJECT]["created_utc"] == mcp_server.WITHHELD_TEXT
    i = list(by_id).index(PROJECT)
    assert all(p.startswith(f"projects[{i}].") for p in res["withheld"]) and res["withheld"]


# ---------------------------------------------------------------------------- NDA errors keep their code only


class StubDaemon:
    """The daemon's answers for one project, through an `httpx.MockTransport`: enough for `add_node`."""

    def __init__(self, root: Path, *, nda: bool, refusal: dict):
        self.head = {"project_id": PROJECT, "root": str(root), "custody": {"nda": nda}, "nodes": 0}
        self.refusal = refusal

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"{API}/projects":
            return httpx.Response(200, json=[self.head])
        if request.method == "POST" and request.url.path == f"{API}/projects/{PROJECT}/nodes":
            return httpx.Response(422, json=self.refusal)
        return httpx.Response(404, json={"error": "not_found"})


@pytest.mark.parametrize("nda", [True, False])
def test_a_rule_refusal_that_quotes_a_label_reaches_an_nda_agent_as_its_codes_only(tmp_path, nda):
    refusal = {
        "errors": ["'ACME lobby' cannot be accepted without a logged full scrub"],
        "violations": [
            {
                "node": "e",
                "code": "SCRUB_REQUIRED",
                "message": "'ACME lobby' cannot be accepted without a logged full scrub",
            },
            {
                "edge": {"source": "c", "target": "e"},
                "code": "NO_PLATE_ANCHOR",
                "message": "ACME concept is no plate",
            },
        ],
    }
    root = tmp_path / PROJECT
    root.mkdir()
    client = httpx.Client(
        base_url=BASE, transport=httpx.MockTransport(StubDaemon(root, nda=nda, refusal=refusal))
    )
    with pytest.raises(McpError) as e:
        PlateTools(PlateBackend(client)).add_node(PROJECT, "Edit", {"engine": "fake"}, "c")
    assert e.value.code == "canvas_rule"
    text = str(e.value)
    if nda:
        assert "ACME" not in text and "lobby" not in text
        assert e.value.message == f"{WITHHELD} (canvas_rule): SCRUB_REQUIRED, NO_PLATE_ANCHOR"
    else:
        assert "ACME lobby" in text
    (row,) = OutboundLog.for_project(root).entries()  # the log holds what was handed over
    assert ("ACME" in row["error"]) is not nda


@needs_room_a
@pytest.mark.parametrize("nda", [True, False])
def test_a_daemon_refusal_that_echoes_project_text_is_withheld_on_nda(daemon, nda):
    c = daemon
    tools = _tools(c)
    _project(c, nda=nda)
    secret = "/Volumes/ACME-CLIENT/recipes/lobby.yaml"
    _put(c, _edit_graph(_packet(c), recipe=secret))
    with pytest.raises(McpError) as e:
        tools.estimate_run(PROJECT, "e")
    assert e.value.code == "unknown_recipe"
    assert ("ACME" in str(e.value)) is not nda
    if nda:
        assert e.value.message == f"{WITHHELD} (unknown_recipe)"
    with pytest.raises(McpError) as e:  # a message the server composed from constants is kept
        tools.run_node(PROJECT, "e", "")
    assert e.value.code == "envelope_required" and "--new-envelope" in e.value.message
    with pytest.raises(McpError) as e:
        tools.run_node(PROJECT, "e", "env_whatever", n=99)
    assert e.value.code == "invalid" and "n must be a whole number" in e.value.message
    assert _runs(c) == []


# ---------------------------------------------------------------------------- the review guard


@needs_room_a
@pytest.mark.parametrize("nda", [False, True])
def test_export_review_refuses_a_gate_fail_take_until_it_is_overridden(sync_settings, auth, nda):
    with _daemon(sync_settings, auth, EchoEngine(shift_px=12)) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        tools = _tools(c)
        _ready(c, nda=nda)
        run_id = tools.run_node(PROJECT, "e", _envelope(c))["data"]["run_id"]
        assert c.get(f"{API}/runs/{run_id}").json()["state"] == "GATE_FAIL"

        r = c.get(f"{API}/runs/{run_id}/reviewable")  # the daemon's answer, the one POST /review gives
        assert r.status_code == 409, r.text
        assert r.json()["error"] == "not_reviewable" and r.json()["code"] == "GATE_FAIL"
        with pytest.raises(McpError) as e:
            tools.export_review(PROJECT, [run_id])
        assert e.value.code == "not_reviewable" and "GATE_FAIL" in str(e.value)
        if nda:
            assert e.value.message == f"{WITHHELD} (not_reviewable): GATE_FAIL"
        assert _zips(sync_settings) == []  # refused before anything was written

        r = c.post(f"{API}/runs/{run_id}/override", json={"reason": REASON})
        assert r.status_code == 200, r.text
        r = c.get(f"{API}/runs/{run_id}/reviewable")
        assert r.status_code == 200 and r.json() == {"ok": True}, r.text
        bundle = tools.export_review(PROJECT, [run_id])
        assert Path(bundle["data"]["bundle_path"]).is_file()
        assert _zips(sync_settings) == [Path(bundle["data"]["bundle_path"])]
        assert bundle.get("nda", False) is nda


@needs_room_a
def test_export_review_refuses_a_take_that_is_not_finished_and_writes_nothing(daemon, sync_settings):
    c = daemon
    tools = _tools(c)
    _ready(c)
    done = tools.run_node(PROJECT, "e", _envelope(c))["data"]["run_id"]
    r = c.post(f"{API}/projects/{PROJECT}/nodes/e/quote", json={})
    assert r.status_code == 200, r.text
    quoted = r.json()["run"]["id"]
    with pytest.raises(McpError) as e:  # one take a client must not see stops the whole bundle
        tools.export_review(PROJECT, [done, quoted])
    assert e.value.code == "not_reviewable" and "RUN_NOT_DELIVERABLE" in str(e.value)
    assert _zips(sync_settings) == []
    assert c.get(f"{API}/runs/run_nope/reviewable").status_code == 404
    assert c.get(f"{API}/runs/{done}/reviewable").json() == {"ok": True}


# ---------------------------------------------------------------------------- concurrency


@needs_room_a
def test_six_concurrent_run_node_calls_on_one_node_are_six_done_attempts(daemon, auth):
    c = daemon
    _ready(c)
    env = _envelope(c)
    start = threading.Barrier(6)

    def one(_i: int) -> str:
        mine = TestClient(c.app, base_url=BASE)  # one client per thread, one daemon
        mine.headers.update(auth)
        tools = _tools(mine)
        start.wait()
        return tools.run_node(PROJECT, "e", env)["data"]["run_id"]

    with ThreadPoolExecutor(6) as pool:
        run_ids = list(pool.map(one, range(6)))
    assert len(set(run_ids)) == 6
    runs = _runs(c)
    assert sorted(r["id"] for r in runs) == sorted(run_ids)
    assert [r["attempt"] for r in runs] == [1, 2, 3, 4, 5, 6]
    assert {r["state"] for r in runs} == {"DONE"}, runs
    assert not [r for r in runs if r["reason_code"] == "duplicate_request"]


def test_twelve_concurrent_add_node_calls_leave_twelve_nodes(daemon, auth):
    c = daemon
    _project(c)
    start = threading.Barrier(12)

    def one(_i: int) -> dict:
        mine = TestClient(c.app, base_url=BASE)
        mine.headers.update(auth)
        tools = _tools(mine)
        start.wait()
        return tools.add_node(PROJECT, "Colour")["data"]

    with ThreadPoolExecutor(12) as pool:
        added = list(pool.map(one, range(12)))
    ids = [a["node_id"] for a in added]
    assert len(set(ids)) == 12 and all(i.startswith("colour-") for i in ids)
    stored = c.get(f"{API}/projects/{PROJECT}/graph").json()
    assert sorted(n["id"] for n in stored["nodes"] if n["kind"] == "Colour") == sorted(ids)
    assert stored["revision"] == 12 and sorted(a["revision"] for a in added) == list(range(1, 13))


# ---------------------------------------------------------------------------- a log failure after the daemon acted


@needs_room_a
@pytest.mark.parametrize("nda", [False, True])
def test_a_log_failure_after_run_node_names_the_run_it_started(daemon, monkeypatch, nda):
    c = daemon
    tools = _tools(c)
    _ready(c, nda=nda)
    env = _envelope(c)

    def unwritable(self, **kw):
        raise PermissionError(13, "Permission denied", "/Volumes/ACME-CLIENT/outbound.jsonl")

    monkeypatch.setattr(outbound_log.OutboundLog, "request", unwritable)
    with pytest.raises(McpError) as e:
        tools.run_node(PROJECT, "e", env)
    assert e.value.code == "outbound_log_failed" and "nothing returned" in e.value.message
    (run,) = _runs(c)
    assert run["state"] == "DONE" and f"run_id {run['id']}" in e.value.message  # it happened: here it is
    assert ("ACME" in e.value.message) is not nda

    with pytest.raises(McpError) as e:
        tools.add_node(PROJECT, "Colour")
    assert e.value.code == "outbound_log_failed"
    stored = c.get(f"{API}/projects/{PROJECT}/graph").json()
    (colour,) = [n["id"] for n in stored["nodes"] if n["kind"] == "Colour"]
    assert f"node_id {colour}" in e.value.message
