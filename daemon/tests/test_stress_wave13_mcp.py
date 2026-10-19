"""Wave 13 stress fixes on the MCP server (router plan T157, decision 91).

Two repros from the mcp-cli-imports attacker, each confirmed by a skeptic on a clean daemon:

- with `outbound.jsonl` unwritable (`chmod 0o444`), `run_node` still started and spent, and the agent was told
  only `outbound_log_failed` afterwards (T146 put the run id in the message, but the run had happened). The
  mutating tools (`MUTATING_TOOLS`: `run_node`, `estimate_run`, `add_node`) probe the log before the daemon is
  asked to act (`PlateTools.record_probe`): three calls are three `outbound_log_failed`, no run, no spend, no
  node. The reads are unchanged: they fail at the post-action `record` and hand over no data;
- `--new-envelope --hours inf` (or `1e12`, `--usd nan`) crashed or made an envelope that never expires:
  `--usd`, `--hours` and `--per-run-usd` are finite, positive and bounded argparse types (exit 2 with usage).

The harness is the one of `test_stress_wave12_mcp.py`: `PlateTools(PlateBackend(TestClient))` against the
daemon's own app on the committed packet room_a, a FakeEngine that echoes the PNG it was sent, and
`PLATE_RUN_WORKERS=0` so an approved run is DONE when the call returns.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from plate_daemon import egress, mcp_server, outbound_log
from plate_daemon.api import create_app
from plate_daemon.config import Settings
from plate_daemon.engine_fake import RESULT_SCHEME, FakeEngine
from plate_daemon.mcp_server import (
    MUTATING_TOOLS,
    McpError,
    PlateBackend,
    PlateTools,
    finite_positive,
)
from plate_daemon.outbound_log import OutboundLog

ROOT = Path(__file__).resolve().parents[2]
ROOM_A = ROOT / "fixtures" / "packets" / "room_a"
BASE = "http://127.0.0.1:4610"
API = "/api/v0"
PROJECT = "plate-fixture"  # room_a's manifest project_id
SECRET_PATH = "/Volumes/ACME-CLIENT/outbound.jsonl"

needs_room_a = pytest.mark.skipif(
    not (ROOM_A / "manifest.json").is_file() or not (ROOM_A / "beauty.exr").is_file(),
    reason="fixtures/packets/room_a not built yet (T2)",
)
needs_posix_modes = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o444 does not stop an append on Windows or for root",
)


class EchoEngine(FakeEngine):
    """A FakeEngine that hands back the PNG it was sent."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent: dict[str, bytes] = {}

    def submit(self, payload, idempotency_key):
        job = super().submit(payload, idempotency_key)
        self.sent[job] = base64.b64decode(payload["inputs"]["image"]["b64"])
        return job

    def fetch(self, result_url):
        super().fetch(result_url)
        return self.sent[result_url[len(RESULT_SCHEME) :]]


# ---------------------------------------------------------------------------- set-up


@pytest.fixture
def sync_settings(plate_env, monkeypatch) -> Settings:
    monkeypatch.setenv("PLATE_SYNC_INGEST", "1")
    monkeypatch.setenv("PLATE_RUN_WORKERS", "0")
    monkeypatch.delenv("PLATE_ROUTER_MODE", raising=False)
    return Settings.load()


@pytest.fixture
def daemon(sync_settings, auth) -> Iterator[TestClient]:
    with TestClient(create_app(sync_settings, EchoEngine()), base_url=BASE) as c:
        c.app.state.pipeline.sleep = lambda _s: None
        c.headers.update(auth)
        yield c


def _tools(c: TestClient) -> PlateTools:
    return PlateTools(PlateBackend(c))


def _project(c: TestClient, *, nda: bool = False) -> None:
    r = c.post(f"{API}/projects", json={"project_id": PROJECT, "custody": {"nda": nda}})
    assert r.status_code == 201, r.text


def _ready(c: TestClient, *, nda: bool = False) -> None:
    """The project with room_a ingested and Plate -> Edit `e` on the fake engine."""
    _project(c, nda=nda)
    manifest = json.loads((ROOM_A / "manifest.json").read_text(encoding="utf-8"))
    r = c.post(
        f"{API}/packets",
        json={"packet_path": str(ROOM_A), "sender": {"name": "test", "version": "0"}},
        headers={"X-Plate-Packet-Version": manifest["packet_version"]},
    )
    assert r.status_code == 202, r.text
    graph = {
        "nodes": [
            {"id": "p", "kind": "Plate", "packetId": r.json()["packet_id"], "tier": "A", "label": "Plate"},
            {"id": "e", "kind": "Edit", "label": "Edit", "params": {"engine": "fake"}},
        ],
        "edges": [{"id": "p->e", "source": "p", "target": "e"}],
    }
    put = c.put(f"{API}/projects/{PROJECT}/graph", json=graph)
    assert put.status_code == 200, put.text


def _envelope(c: TestClient, *, total: float = 10.0) -> str:
    body = {"project_id": PROJECT, "owner": "agent:test", "max_total_usd": total}
    r = c.post(f"{API}/envelopes", json={**body, "max_per_run_usd": total, "ttl_s": 3600})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _runs(c: TestClient) -> list[dict]:
    r = c.get(f"{API}/projects/{PROJECT}/runs")
    assert r.status_code == 200, r.text
    return r.json()


def _nodes(c: TestClient) -> list[dict]:
    return c.get(f"{API}/projects/{PROJECT}/graph").json()["nodes"]


def _spent(c: TestClient, envelope_id: str) -> float:
    return c.get(f"{API}/envelopes/{envelope_id}").json()["spent_usd"]


def _envelope_rows(c: TestClient) -> int:
    with c.app.state.index.project(PROJECT) as conn:
        return conn.execute("SELECT COUNT(*) FROM budget_envelopes").fetchone()[0]


# ---------------------------------------------------------------------------- the probe


def test_the_probe_opens_for_append_creates_the_parents_and_writes_nothing(tmp_path):
    log = OutboundLog(tmp_path / "deep" / "er" / "outbound.jsonl")
    log.probe()
    assert log.path.is_file() and log.path.stat().st_size == 0
    if sys.platform != "win32":
        assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    log.path.write_bytes(b'{"kind":"request","id":"x"}\n')
    log.probe()  # an existing log is neither truncated nor extended
    assert log.path.read_bytes() == b'{"kind":"request","id":"x"}\n'


def test_the_probe_raises_the_oserror_an_append_would(tmp_path):
    (tmp_path / "not-a-folder").write_text("a file where the project folder should be", encoding="utf-8")
    with pytest.raises(OSError):
        OutboundLog(tmp_path / "not-a-folder" / "outbound.jsonl").probe()
    (tmp_path / "is-a-folder").mkdir()
    with pytest.raises(OSError):
        OutboundLog(tmp_path / "is-a-folder").probe()  # the log path itself is a directory


def test_egress_and_the_mcp_server_share_one_outbound_log_class():
    assert egress.OutboundLog is outbound_log.OutboundLog and hasattr(egress.OutboundLog, "probe")
    assert MUTATING_TOOLS == {"run_node", "estimate_run", "add_node"}
    assert MUTATING_TOOLS <= set(mcp_server.TOOL_NAMES)


@needs_room_a
@pytest.mark.parametrize("tool", sorted(MUTATING_TOOLS))
@pytest.mark.parametrize("nda", [False, True])
def test_a_failed_probe_stops_the_tool_before_any_post_reaches_the_daemon(daemon, monkeypatch, tool, nda):
    c = daemon
    tools = _tools(c)
    _ready(c, nda=nda)
    env = _envelope(c)
    nodes = len(_nodes(c))
    posts: list[tuple[str, str]] = []
    real = c.request

    def spy(method, url, **kw):
        if method.upper() != "GET":
            posts.append((method, url))
        return real(method, url, **kw)

    def unwritable(self):
        raise PermissionError(13, "Permission denied", SECRET_PATH)

    monkeypatch.setattr(c, "request", spy)
    monkeypatch.setattr(OutboundLog, "probe", unwritable)
    calls = {
        "run_node": lambda: tools.run_node(PROJECT, "e", env),
        "estimate_run": lambda: tools.estimate_run(PROJECT, "e"),
        "add_node": lambda: tools.add_node(PROJECT, "Colour"),
    }
    with pytest.raises(McpError) as e:
        calls[tool]()
    assert e.value.code == "outbound_log_failed" and "nothing returned" in e.value.message
    assert ("ACME" in e.value.message) is not nda  # an NDA session sees the type, never the path
    assert "run_id" not in e.value.message and "node_id" not in e.value.message  # nothing was produced
    assert posts == [] and _runs(c) == [] and len(_nodes(c)) == nodes and _spent(c, env) == 0


@needs_room_a
def test_the_reads_are_not_probed(daemon, monkeypatch):
    c = daemon
    tools = _tools(c)
    _ready(c)

    def unwritable(self):
        raise AssertionError("a read must not probe the log")

    monkeypatch.setattr(OutboundLog, "probe", unwritable)
    assert tools.list_projects()["data"]["projects"]
    assert {n["id"] for n in tools.get_graph(PROJECT)["data"]["graph"]["nodes"]} >= {"p", "e"}


@needs_room_a
@needs_posix_modes
def test_an_unwritable_outbound_log_stops_run_node_estimate_and_add_node_and_reads_nothing(
    daemon, sync_settings
):
    """The attacker's repro: `chmod 0o444 outbound.jsonl`, then three `run_node` calls."""
    c = daemon
    tools = _tools(c)
    _ready(c)
    env = _envelope(c)
    tools.get_graph(PROJECT)  # a normal call leaves the log there to be locked
    log = sync_settings.projects_dir / PROJECT / "outbound.jsonl"
    assert log.is_file()
    nodes = len(_nodes(c))
    log.chmod(0o444)
    try:
        for _ in range(3):
            with pytest.raises(McpError) as e:
                tools.run_node(PROJECT, "e", env)
            assert e.value.code == "outbound_log_failed" and "run_id" not in e.value.message
        assert _runs(c) == [] and _spent(c, env) == 0

        with pytest.raises(McpError) as e:
            tools.estimate_run(PROJECT, "e")
        assert e.value.code == "outbound_log_failed" and _runs(c) == []

        for _ in range(2):
            with pytest.raises(McpError) as e:
                tools.add_node(PROJECT, "Colour")
            assert e.value.code == "outbound_log_failed" and "node_id" not in e.value.message
        assert len(_nodes(c)) == nodes  # the graph keeps its node count

        with pytest.raises(McpError) as e:  # a read fails at the record, unchanged, and hands over no data
            tools.get_graph(PROJECT)
        assert e.value.code == "outbound_log_failed"
        assert "nodes" not in e.value.message and "edges" not in e.value.message
    finally:
        log.chmod(0o644)

    data = tools.run_node(PROJECT, "e", env)["data"]  # the mode restored, the tool works again
    (run,) = _runs(c)
    assert run["id"] == data["run_id"] and run["state"] == "DONE"
    assert tools.add_node(PROJECT, "Colour")["data"]["node_id"].startswith("colour-")


# ---------------------------------------------------------------------------- finite --new-envelope arguments


def test_finite_positive_accepts_only_a_finite_number_above_lo_and_up_to_hi():
    parse = finite_positive(0, 10)
    assert parse("5") == 5.0 and parse("10") == 10.0 and parse("0.001") == 0.001 and parse("1e1") == 10.0
    for bad in ("inf", "-inf", "nan", "0", "-1", "10.0001", "1e12", "", "abc"):
        with pytest.raises(argparse.ArgumentTypeError) as e:
            parse(bad)
        assert str(e.value) == f"{bad!r}: must be a finite number between 0 and 10"


def _envelope_cmd(*extra: str) -> list[str]:
    return ["--new-envelope", "--project", PROJECT, *extra]


@pytest.mark.parametrize(
    ("args", "option"),
    [
        (("--usd", "1", "--hours", "inf"), "--hours"),
        (("--usd", "1", "--hours", "1e12"), "--hours"),
        (("--usd", "1", "--hours", "8785"), "--hours"),
        (("--usd", "1", "--hours", "-3"), "--hours"),
        (("--usd", "1", "--hours", "nan"), "--hours"),
        (("--usd", "inf", "--hours", "1"), "--usd"),
        (("--usd", "nan", "--hours", "1"), "--usd"),
        (("--usd", "0", "--hours", "1"), "--usd"),
        (("--usd", "-1", "--hours", "1"), "--usd"),
        (("--usd", "1000000.5", "--hours", "1"), "--usd"),
        (("--usd", "abc", "--hours", "1"), "--usd"),
        (("--usd", "1", "--hours", "1", "--per-run-usd", "inf"), "--per-run-usd"),
        (("--usd", "1", "--hours", "1", "--per-run-usd", "0"), "--per-run-usd"),
        (("--usd", "1", "--hours", "1", "--per-run-usd", "2e6"), "--per-run-usd"),
    ],
)
def test_a_non_finite_or_out_of_range_envelope_argument_is_a_usage_error_and_makes_nothing(
    daemon, capsys, args, option
):
    _project(daemon)
    with pytest.raises(SystemExit) as e:
        mcp_server.main(_envelope_cmd(*args))
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert option in err and "must be a finite number between" in err and "usage:" in err
    assert _envelope_rows(daemon) == 0


def test_finite_envelope_arguments_make_the_envelope(daemon, capsys):
    c = daemon
    _project(c)
    assert mcp_server.main(_envelope_cmd("--usd", "1", "--hours", "8")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["project_id"] == PROJECT and doc["max_total_usd"] == 1 and doc["max_per_run_usd"] == 1
    got: dict[str, Any] = c.get(f"{API}/envelopes/{doc['envelope_id']}").json()
    assert got["max_total_usd"] == 1

    # the limits themselves are allowed: a million dollars for a leap year
    top = ("--usd", "1000000", "--hours", "8784", "--per-run-usd", "0.5")
    assert mcp_server.main(_envelope_cmd(*top)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["max_total_usd"] == 1_000_000 and doc["max_per_run_usd"] == 0.5
    assert _envelope_rows(c) == 2
