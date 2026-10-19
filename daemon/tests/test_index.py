"""index.py: the per-project SQLite cache (WAL, M002 schema) and its rebuild from the project folder.

M004 (router plan T152) adds `packet_router`, the render-it router's inputs per packet: a cache the rebuild leaves
empty (the next quote refills it)."""

from __future__ import annotations

import json
import sqlite3

from plate_daemon.db import connect, tx
from plate_daemon.index import COUNTED_TABLES, Index, apply_packet_entry, packet_entry
from plate_daemon.machine import create_run, transition
from plate_daemon.migrations import M001, MIGRATIONS
from plate_daemon.models import Route, RunCreate
from plate_daemon.projects import Project
from plate_daemon.sse import Bus
from plate_daemon.states import RunState
from plate_daemon.timeutil import iso, utcnow

FOLDER_TABLES = ("assets", "packets", "runs")
ROUTE = Route(engine_id="nano-banana-2.1", provider="google", region="global")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})")}


def _append(project: Project, rec: dict) -> None:
    with open(project.runs_jsonl, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec) + "\n")


def _live_project(tmp_path, home) -> tuple[Project, Index, dict[str, str]]:
    """A project written the way the daemon writes it: index rows and folder records side by side."""
    project = Project.create(tmp_path / "nas" / "tula-wot", "tula-wot", {"nda": True})
    idx = Index(home)
    idx.set_project_root("tula-wot", project.root, project.custody)
    cas = project.cas(on_put=lambda h, e, n: idx.record_asset("tula-wot", h, e, n))
    keys = [cas.put(b"plate", "exr"), cas.put(b"mask", "png"), cas.put(b"result", "png")]
    states: dict[str, str] = {}
    with idx.project("tula-wot") as conn:
        for n, packet_id in enumerate(("pkt_a", "pkt_b")):
            entry = {
                "shot_id": f"s0{n}",
                "kind": "still",
                "tier": "A",
                "packet_version": "0.1.1",
                "path": str(tmp_path / "packets" / packet_id),
                "manifest_blake3": keys[0] if n == 0 else "b" * 64,
                "view_hash": "c" * 64,
            }
            with tx(conn):
                apply_packet_entry(conn, "tula-wot", packet_id, entry)
            project.register_packet(packet_id, packet_entry(conn, packet_id))
        for packet_id, steps in (
            ("pkt_a", [RunState.QUOTED, RunState.APPROVED]),
            ("pkt_b", [RunState.CANCELLED]),
        ):
            run = create_run(conn, RunCreate(project_id="tula-wot", packet_id=packet_id, route=ROUTE))
            _append(
                project,
                {
                    "run_id": run.id,
                    "kind": "created",
                    "at": iso(utcnow()),
                    "packet_id": packet_id,
                    "node_id": f"node-{packet_id}",
                    "route": ROUTE.model_dump(),
                },
            )
            for to in steps:
                transition(conn, run.id, to, actor="human")
                rec = {"run_id": run.id, "kind": to.value.lower(), "at": iso(utcnow())}
                if to is RunState.QUOTED:
                    rec["data"] = {"quote": {"engine_id": ROUTE.engine_id, "cost_usd_quoted": 0.04}}
                _append(project, rec)
            states[run.id] = steps[-1].value
    Bus(idx).publish(None, {"kind": "graph.saved"}, project_id="tula-wot")  # the SSE log is not folder truth
    return project, idx, states


def test_connections_are_wal_with_the_plan_pragmas(settings):
    with Index(settings.home).project("tula-wot", create=True) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert (settings.home / "index" / "tula-wot.sqlite").is_file()


def test_schema_has_the_plan_columns(settings):
    with Index(settings.home).project("tula-wot", create=True) as conn:
        assert _columns(conn, "assets") == {"hash", "ext", "bytes", "phash", "added_utc"}
        run_cols = {
            "run_id",
            "node_id",
            "state",
            "priority",
            "idempotency_key",
            "provider_job_id",
            "quote_json",
        }
        assert run_cols | {"updated_utc"} <= _columns(conn, "runs")
        assert {"packet_id", "path", "view_hash", "scene_hash", "manifest_hash"} <= _columns(conn, "packets")
        assert _columns(conn, "events") == {"seq", "run_id", "at", "kind", "json"}
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == [v for v, _ in MIGRATIONS]
        assert _columns(conn, "ledger_outbox") == {
            "id",
            "run_id",
            "kind",
            "record",
            "created_utc",
            "attempts",
            "last_error",
        }
        assert _columns(conn, "packet_router") == {"packet_id", "version", "json", "computed_utc"}


def test_a_seed_m001_index_migrates_forward_without_loss(settings):
    path = settings.home / "index" / "old.sqlite"
    conn = connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_utc TEXT NOT NULL)")
    with tx(conn):
        for stmt in M001:
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_migrations VALUES (1, ?)", (iso(utcnow()),))
    with tx(conn):
        conn.execute(  # the seed's M001 columns only: no view_hash yet
            "INSERT INTO packets (packet_id, project_id, shot_id, kind, tier, packet_version, path, manifest_blake3,"
            " status, received_utc) VALUES ('pkt_old', 'old', 's', 'still', 'A', '0.1.1', '/p', ?, 'ingested', ?)",
            ("d" * 64, iso(utcnow())),
        )
    conn.close()
    with Index(settings.home).project("old") as conn:
        row = conn.execute("SELECT manifest_blake3, manifest_hash FROM packets").fetchone()
        assert tuple(row) == ("d" * 64, "d" * 64)
        run = create_run(conn, RunCreate(project_id="old", packet_id="pkt_old", route=ROUTE))
        assert conn.execute("SELECT run_id FROM runs WHERE id = ?", (run.id,)).fetchone()[0] == run.id


def test_rebuild_equals_the_live_index_and_a_fresh_one(tmp_path, settings):
    project, idx, states = _live_project(tmp_path, settings.home)
    live = idx.counts("tula-wot")
    assert {t: live[t] for t in FOLDER_TABLES} == {"assets": 3, "packets": 2, "runs": 2}
    assert live["events"] == 1
    # Make the cache stale: an asset row with no object behind it.
    idx.record_asset("tula-wot", "f" * 64, "png", 1)

    rebuilt = idx.rebuild_from_project(project.root, verify=True)
    assert rebuilt["problems"] == []
    assert {t: rebuilt[t] for t in FOLDER_TABLES} == {t: live[t] for t in FOLDER_TABLES}
    assert rebuilt["events"] == 0  # not folder truth: write it to the folder first

    fresh = Index(tmp_path / "other-machine").rebuild_from_project(project.root)
    assert {t: fresh[t] for t in COUNTED_TABLES} == {t: rebuilt[t] for t in COUNTED_TABLES}

    with idx.project("tula-wot") as conn:
        got = {r["id"]: r["state"] for r in conn.execute("SELECT id, state FROM runs")}
        nodes = {r["node_id"] for r in conn.execute("SELECT node_id FROM runs")}
        quote = conn.execute("SELECT quote_json FROM runs WHERE state = 'APPROVED'").fetchone()[0]
    assert got == states
    assert nodes == {"node-pkt_a", "node-pkt_b"}
    assert json.loads(quote)["cost_usd_quoted"] == 0.04
    assert idx.project_root("tula-wot") == project.root.absolute()
    assert idx.locate_packet("pkt_b") == "tula-wot"


def test_rebuild_reports_what_the_folder_cannot_back(tmp_path, settings):
    project = Project.create(tmp_path / "p", "proj", {"nda": False})
    project.cas().put(b"x", "png")
    _append(project, {"run_id": "run_orphan", "kind": "created", "packet_id": "pkt_missing"})
    with open(project.runs_jsonl, "a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"run_id": "run_torn", "ki')  # a crash mid-append
    out = Index(settings.home).rebuild_from_project(project.root)
    assert out["assets"] == 1 and out["runs"] == 0
    assert any("pkt_missing" in p for p in out["problems"])
    assert any("torn last line" in p for p in out["problems"])


def test_packet_router_rows_name_an_indexed_packet_and_a_rebuild_drops_them(tmp_path, settings):
    project, idx, _states = _live_project(tmp_path, settings.home)
    with idx.project("tula-wot") as conn:
        with tx(conn):
            conn.execute(
                "INSERT INTO packet_router (packet_id, version, json, computed_utc) VALUES (?, ?, ?, ?)",
                ("pkt_a", "2026.10.19", json.dumps({"verdict": "ai_by_region"}), iso(utcnow())),
            )
        try:
            with tx(conn):
                conn.execute(
                    "INSERT INTO packet_router (packet_id, version, json, computed_utc) VALUES (?, ?, ?, ?)",
                    ("pkt_never_sent", "2026.10.19", "{}", iso(utcnow())),
                )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("a router row for a packet the index does not hold was accepted")
        assert conn.execute("SELECT COUNT(*) FROM packet_router").fetchone()[0] == 1
    out = idx.rebuild_from_project(project.root)
    assert out["problems"] == [] and out["packets"] == 2
    with idx.project("tula-wot") as conn:
        rows = conn.execute("SELECT COUNT(*) FROM packet_router").fetchone()[0]
    assert rows == 0  # a cache, not folder truth
