"""Wave 12 (T134): the ledger outbox, the memoised read, and a spend that survives a lost `submitted`.

The stress runs found the ledger dropping records when `runs.jsonl` was unwritable or the project lock was
held for longer than five seconds. The record is now parked in the project index (`ledger_outbox`) and
appended later, in order; reads of an unchanged file stop re-parsing it; and a run that has a provider job
id or a later record counts as submitted even when its own `submitted` line never made it to disk.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from plate_daemon import ledger as ledger_mod
from plate_daemon.db import open_index, tx
from plate_daemon.ledger import (
    FIELD_ORDER,
    GENESIS,
    Ledger,
    LedgerError,
    RunView,
    drain_outbox,
    enqueue_outbox,
    fold_runs,
    outbox_count,
    seal,
    summarize_records,
)
from plate_daemon.migrations import MIGRATIONS

T0 = datetime(2026, 10, 18, 9, 0, 0, tzinfo=UTC)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def led(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "proj")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = open_index(tmp_path / "index" / "p.sqlite")
    yield c
    c.close()


def rec(kind: str = "quoted", run_id: str = "r1", n: float = 0, **extra) -> dict:
    return {"kind": kind, "run_id": run_id, "at": at(n), **extra}


def write_chain(path: Path, n: int) -> str:
    """A valid `runs.jsonl` of `n` records, written in one go (n fsyncs would dominate the test)."""
    prev, out = GENESIS, []
    for i in range(n):
        body = {
            "v": 1,
            "seq": i + 1,
            "prev_hash": prev,
            "at": at(i),
            "kind": "quoted" if i % 3 == 0 else "submitted",
            "run_id": f"run_{i % 400}",
            "node_id": f"node_{i % 20}",
            "engine_id": "nano-banana-2.1",
            "cost_usd_quoted": 0.04,
            "data": {"model": "gemini-3.1-flash-image", "task": "edit", "shot_class": "hero"},
        }
        sealed = seal(prev, body)
        ordered = {k: (sealed if k == "hash" else body[k]) for k in FIELD_ORDER if k == "hash" or k in body}
        out.append(json.dumps(ordered, separators=(",", ":")))
        prev = sealed
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(out) + "\n").encode("ascii"))
    return prev


def read_only(path: Path) -> bool:
    """Make `path` unwritable; False when this process can write anyway (root)."""
    os.chmod(path, 0o444)
    if os.access(path, os.W_OK):
        os.chmod(path, 0o644)
        return False
    return True


# ---------------------------------------------------------------------------- the table


def test_migration_adds_the_outbox_table_with_the_planned_columns(conn: sqlite3.Connection):
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(ledger_outbox)")}
    assert list(cols) == ["id", "run_id", "kind", "record", "created_utc", "attempts", "last_error"]
    assert cols["id"]["pk"] == 1
    assert cols["kind"]["notnull"] == cols["record"]["notnull"] == cols["created_utc"]["notnull"] == 1
    assert cols["attempts"]["notnull"] == 1 and cols["attempts"]["dflt_value"] == "0"
    assert cols["run_id"]["notnull"] == cols["last_error"]["notnull"] == 0
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == [v for v, _ in MIGRATIONS] and versions[-1] >= 3


def test_an_index_made_before_the_migration_gains_the_table(tmp_path: Path):
    from plate_daemon.db import applied_versions, connect

    path = tmp_path / "index" / "old.sqlite"
    c = connect(path)
    applied_versions(c)
    for version, statements in MIGRATIONS[:2]:  # an index at the M002 level
        with tx(c):
            for stmt in statements:
                c.execute(stmt)
            c.execute("INSERT INTO schema_migrations (version, applied_utc) VALUES (?, 'x')", (version,))
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name = 'ledger_outbox'").fetchone() is None
    c.close()
    c = open_index(path)
    assert outbox_count(c) == 0
    c.close()


# ---------------------------------------------------------------------------- enqueue and drain


def test_enqueue_returns_increasing_ids_and_counts(conn: sqlite3.Connection):
    ids = [enqueue_outbox(conn, rec(n=i, run_id=f"r{i}")) for i in range(3)]
    assert ids == sorted(ids) and len(set(ids)) == 3 and all(isinstance(i, int) for i in ids)
    assert outbox_count(conn) == 3
    row = conn.execute("SELECT * FROM ledger_outbox ORDER BY id").fetchone()
    assert (row["run_id"], row["kind"], row["attempts"], row["last_error"]) == ("r0", "quoted", 0, None)
    assert json.loads(row["record"])["at"] == at(0)


def test_enqueue_checks_the_record_like_append_and_stamps_a_missing_time(conn: sqlite3.Connection):
    with pytest.raises(LedgerError):
        enqueue_outbox(conn, {"kind": "not-a-kind"})
    with pytest.raises(LedgerError):
        enqueue_outbox(conn, {"kind": "done", "seq": 4})
    with pytest.raises(LedgerError):
        enqueue_outbox(conn, {"kind": "billed", "cost_usd_billed": float("nan")})
    assert outbox_count(conn) == 0
    enqueue_outbox(conn, {"kind": "done", "run_id": "r1"}, clock=lambda: T0)
    assert json.loads(conn.execute("SELECT record FROM ledger_outbox").fetchone()[0])["at"] == at(0)


def test_enqueue_joins_the_callers_transaction(conn: sqlite3.Connection):
    with pytest.raises(RuntimeError), tx(conn):
        enqueue_outbox(conn, rec())
        raise RuntimeError("the surrounding write failed")
    assert outbox_count(conn) == 0
    with tx(conn):
        enqueue_outbox(conn, rec())
    assert outbox_count(conn) == 1


def test_drain_appends_in_row_order_and_empties_the_outbox(led: Ledger, conn: sqlite3.Connection):
    led.append(rec("quoted", "r0", 0, cost_usd_quoted=0.04))
    for i in range(1, 4):
        enqueue_outbox(conn, rec("approved", f"r{i}", i, cost_usd_quoted=0.04 * i, data={"i": i}))
    assert drain_outbox(conn, led) == 3
    assert outbox_count(conn) == 0
    got = led.records()
    assert [r["run_id"] for r in got] == ["r0", "r1", "r2", "r3"]
    assert [r["seq"] for r in got] == [1, 2, 3, 4]
    assert got[3]["data"] == {"i": 3} and got[3]["cost_usd_quoted"] == pytest.approx(0.12)
    assert led.verify() == (True, None)
    assert drain_outbox(conn, led) == 0


def test_an_unwritable_ledger_parks_records_and_drains_them_after_the_mode_is_restored(
    led: Ledger, conn: sqlite3.Connection
):
    led.append(rec("quoted", "r1", 0, cost_usd_quoted=0.04))
    if not read_only(led.path):
        pytest.skip("this process can write through a 0o444 file (running as root)")
    try:
        with pytest.raises(OSError):
            led.append(rec("submitted", "r1", 1, provider_job_id="job_1"))
        for i, kind in enumerate(("submitted", "fetched", "done"), start=1):
            enqueue_outbox(conn, rec(kind, "r1", i, provider_job_id="job_1"))
        # Still read-only: the first row fails, is marked, and nothing behind it overtakes it.
        assert drain_outbox(conn, led) == 0
        assert outbox_count(conn) == 3
        first = conn.execute("SELECT attempts, last_error FROM ledger_outbox ORDER BY id").fetchall()
        assert first[0]["attempts"] == 1 and first[0]["last_error"]
        assert first[1]["attempts"] == first[2]["attempts"] == 0
    finally:
        os.chmod(led.path, 0o644)
    led.append(rec("billed", "r1", 4, cost_usd_billed=0.04))  # a later record from the normal path
    assert drain_outbox(conn, led) == 3
    assert outbox_count(conn) == 0
    assert [r["kind"] for r in led.records()] == ["quoted", "billed", "submitted", "fetched", "done"]
    assert led.verify() == (True, None)
    assert led.check().count == 5


def test_the_first_failure_stops_the_drain_and_the_rest_wait(
    led: Ledger, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
):
    for i in range(3):
        enqueue_outbox(conn, rec("done", f"r{i}", i))
    real = Ledger.append
    calls: list[str] = []

    def flaky(self, record=None, /, **fields):
        calls.append((record or fields)["run_id"])
        if len(calls) == 2:
            raise OSError("disk went away")
        return real(self, record, **fields)

    monkeypatch.setattr(Ledger, "append", flaky)
    assert drain_outbox(conn, led) == 1  # r0 appended, r1 failed, r2 never tried
    assert calls == ["r0", "r1"]
    rows = conn.execute("SELECT run_id, attempts, last_error FROM ledger_outbox ORDER BY id").fetchall()
    assert [r["run_id"] for r in rows] == ["r1", "r2"]
    assert rows[0]["attempts"] == 1 and "disk went away" in rows[0]["last_error"]
    assert rows[1]["attempts"] == 0
    monkeypatch.setattr(Ledger, "append", real)
    assert drain_outbox(conn, led) == 2
    assert [r["run_id"] for r in led.records()] == ["r0", "r1", "r2"]


class FirstDeleteFails:
    """A connection whose first `DELETE FROM ledger_outbox` raises `database is locked`, as a busy index would."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.failed = False

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def execute(self, sql: str, *args):
        if sql.startswith("DELETE FROM ledger_outbox") and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)


def test_a_row_whose_delete_fails_after_the_append_is_not_appended_twice(
    led: Ledger, conn: sqlite3.Connection
):
    enqueue_outbox(conn, rec("billed", "r1", 1, cost_usd_billed=0.04))
    flaky = FirstDeleteFails(conn)
    assert drain_outbox(flaky, led) == 1  # the delete was retried outside the rolled-back transaction
    assert flaky.failed and outbox_count(conn) == 0
    assert drain_outbox(conn, led) == 0
    assert [r["kind"] for r in led.records()] == ["billed"]


def test_a_row_the_json_cannot_be_read_from_is_marked_and_blocks_later_rows(
    led: Ledger, conn: sqlite3.Connection
):
    enqueue_outbox(conn, rec("done", "r1"))
    enqueue_outbox(conn, rec("done", "r2"))
    with tx(conn):
        conn.execute(
            "UPDATE ledger_outbox SET record = '{not json' WHERE id = (SELECT MIN(id) FROM ledger_outbox)"
        )
    assert drain_outbox(conn, led) == 0
    bad = conn.execute("SELECT attempts, last_error FROM ledger_outbox ORDER BY id").fetchone()
    assert bad["attempts"] == 1 and bad["last_error"]
    assert led.records() == []


def test_the_outbox_survives_closing_the_index(tmp_path: Path, led: Ledger):
    path = tmp_path / "index" / "again.sqlite"
    c = open_index(path)
    enqueue_outbox(c, rec("done", "r1"))
    c.close()
    c = open_index(path)
    assert outbox_count(c) == 1
    assert drain_outbox(c, led) == 1
    c.close()


# ---------------------------------------------------------------------------- the memoised read


def test_records_on_an_unchanged_10000_record_file_is_fast_the_second_time(led: Ledger):
    head = write_chain(led.path, 10_000)
    first = led.records()
    assert len(first) == 10_000 and first[-1]["hash"] == head
    best = float("inf")
    for _ in range(5):  # the best of five: one slow scheduler tick is not a slow read
        t = time.perf_counter()
        again = led.records()
        best = min(best, time.perf_counter() - t)
    assert len(again) == 10_000
    assert best < 0.005, f"second records() took {best * 1000:.1f} ms"


def test_records_sees_a_record_another_ledger_instance_appended(led: Ledger):
    write_chain(led.path, 50)
    assert len(led.records()) == 50
    other = Ledger(led.project_dir)
    added = other.append(rec("done", "late", 99, data={"x": 1}))
    got = led.records()
    assert len(got) == 51
    assert got[-1]["hash"] == added["hash"] and got[-1]["run_id"] == "late"
    assert led.verify() == (True, None)


def test_append_extends_the_memo_instead_of_reading_the_file_again(
    led: Ledger, monkeypatch: pytest.MonkeyPatch
):
    write_chain(led.path, 20)
    led.records()
    scans = []
    real_scan = ledger_mod._scan
    monkeypatch.setattr(ledger_mod, "_scan", lambda data: scans.append(len(data)) or real_scan(data))
    for i in range(5):
        led.append(rec("done", f"n{i}", 100 + i))
        assert len(led.records()) == 21 + i
    assert led.records()[-1]["run_id"] == "n4"
    # `_tail_state` is allowed to scan once when the file is not what this instance last wrote; reads are not.
    assert len(scans) <= 1
    assert led.records() == Ledger(led.project_dir).records()


def test_a_ledger_that_never_read_the_file_still_reads_it_after_appending(led: Ledger):
    led.append(rec("quoted", "a", 0))
    led.append(rec("done", "a", 1))
    assert [r["kind"] for r in led.records()] == ["quoted", "done"]
    fresh = Ledger(led.project_dir)
    fresh.append(rec("billed", "a", 2))  # memo never built on this instance: append must not invent one
    assert [r["kind"] for r in fresh.records()] == ["quoted", "done", "billed"]


def test_an_edit_that_changes_the_file_is_seen(led: Ledger):
    write_chain(led.path, 10)
    assert len(led.records()) == 10
    time.sleep(0.01)  # coarse mtimes: the size changes too, but keep the test honest on any filesystem
    lines = led.path.read_bytes().split(b"\n")
    led.path.write_bytes(b"\n".join(lines[:5]) + b"\n")
    assert len(led.records()) == 5
    led.path.unlink()
    assert led.records() == []
    led.append(rec("quoted", "z", 0))
    assert [r["run_id"] for r in led.records()] == ["z"]


def test_a_caller_changing_a_returned_record_does_not_change_the_next_read(led: Ledger):
    led.append(rec("quoted", "r1", 0, cost_usd_quoted=0.04))
    got = led.records()
    got[0]["kind"] = "tampered"
    got[0]["cost_usd_quoted"] = 99
    got.clear()
    again = led.records()
    assert len(again) == 1 and again[0]["kind"] == "quoted" and again[0]["cost_usd_quoted"] == 0.04
    assert led.read()[1] == []


def test_a_torn_tail_is_reported_then_repaired_and_the_memo_follows(led: Ledger):
    led.append(rec("quoted", "r1", 0))
    with open(led.path, "ab") as fh:
        fh.write(b'{"v":1,"seq":2,"pre')
    records, problems = led.read()
    assert len(records) == 1 and len(problems) == 1 and "torn" in problems[0]
    led.append(rec("done", "r1", 1))  # moves the torn bytes aside and truncates
    records, problems = led.read()
    assert [r["kind"] for r in records] == ["quoted", "done"] and problems == []
    assert led.repairs and led.verify() == (True, None)
    assert records == Ledger(led.project_dir).records()


def test_two_threads_reading_while_one_appends_never_see_a_half_record(led: Ledger):
    import threading

    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            got = led.records()
            if [r["seq"] for r in got] != list(range(1, len(got) + 1)):
                errors.append("gap")

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for i in range(40):
        led.append(rec("done", f"r{i}", i))
    stop.set()
    for t in threads:
        t.join()
    assert errors == [] and len(led.records()) == 40


# ---------------------------------------------------------------------------- submitted by evidence


def test_a_run_with_no_submitted_record_but_later_ones_counts_as_one_generation_at_its_approved_price():
    records = [
        rec("quoted", "r1", 0, cost_usd_quoted=0.04, engine_id="nano-banana-2.1", custody_grade="C1"),
        rec("approved", "r1", 1, cost_usd_quoted=0.05, engine_id="nano-banana-2.1", custody_grade="C1"),
        rec("fetched", "r1", 2, engine_id="nano-banana-2.1", custody_grade="C1"),
        rec("done", "r1", 3, engine_id="nano-banana-2.1", custody_grade="C1"),
    ]
    assert "submitted" not in {r["kind"] for r in records}
    s = summarize_records(records)
    assert s["generations"] == 1
    assert s["spent_usd_quoted"] == 0.05
    assert s["by_engine"]["nano-banana-2.1"]["generations"] == 1
    assert s["by_custody"]["C1"]["spent_usd_quoted"] == 0.05
    assert fold_runs(records)["r1"].submitted is True


@pytest.mark.parametrize(
    ("extra_kind", "extra"),
    [
        ("fetched", {}),
        ("gated", {}),
        ("done", {}),
        ("failed", {"provider_job_id": "job_9"}),
        ("billed", {"provider_job_id": "job_9", "cost_usd_billed": 0.05}),
    ],
)
def test_each_piece_of_evidence_alone_marks_the_run_submitted(extra_kind: str, extra: dict):
    records = [rec("quoted", "r1", 0, cost_usd_quoted=0.04), rec(extra_kind, "r1", 1, **extra)]
    view = fold_runs(records)["r1"]
    assert view.submitted is True
    assert summarize_records(records)["generations"] == 1


@pytest.mark.parametrize(
    "kind", ["quoted", "approved", "cancelled", "expired", "refused", "verdict", "export"]
)
def test_nothing_that_happens_before_the_vendor_marks_the_run_submitted(kind: str):
    records = [rec("quoted", "r1", 0, cost_usd_quoted=0.04), rec(kind, "r1", 1)]
    assert fold_runs(records)["r1"].submitted is False
    s = summarize_records(records)
    assert s["generations"] == 0 and s["spent_usd_quoted"] == 0


@pytest.mark.parametrize(
    "engine_id",
    [
        "import:manual",
        "import:higgsfield",
        "carry-over",
        "plate-numeric",
        "plate-plugin",
        "local-recomposite",
    ],
)
def test_runs_that_never_call_a_vendor_stay_out_of_the_spend_table(engine_id: str):
    records = [
        rec("quoted", "r1", 0, cost_usd_quoted=2.5, engine_id=engine_id),
        rec("approved", "r1", 1, cost_usd_quoted=2.5, engine_id=engine_id),
        rec("done", "r1", 2, engine_id=engine_id, provider_job_id="import-0123456789abcdef01234567"),
    ]
    assert fold_runs(records)["r1"].submitted is False
    s = summarize_records(records)
    assert s["generations"] == 0 and s["spent_usd_quoted"] == 0 and s["by_engine"] == {}


def test_the_local_engine_prefixes_cover_every_engine_that_never_calls_a_vendor():
    from plate_daemon import imports, lineage, revisions
    from plate_daemon.ledger import LOCAL_ENGINE_PREFIXES
    from plate_daemon.nodes import common

    local = [
        *lineage.NON_AI_ENGINES,
        f"{imports.ENGINE_PREFIX}manual",
        f"{imports.ENGINE_PREFIX}runway",
        revisions.CARRY_ENGINE_ID,
        common.NUMERIC_ENGINE_ID,
        common.PLUGIN_ENGINE_ID,
    ]
    assert all(e.startswith(LOCAL_ENGINE_PREFIXES) for e in local)
    assert not any("fake".startswith(p) or "nano-banana-2.1".startswith(p) for p in LOCAL_ENGINE_PREFIXES)


def test_the_explicit_submitted_record_still_counts_and_the_view_default_is_not_submitted():
    assert RunView("r0").submitted is False
    records = [rec("quoted", "r1", 0, cost_usd_quoted=0.04), rec("submitted", "r1", 1)]
    assert summarize_records(records)["generations"] == 1


def test_the_approved_price_wins_and_the_quoted_one_is_the_fallback():
    approved_higher = [
        rec("quoted", "r1", 0, cost_usd_quoted=0.04),
        rec("approved", "r1", 1, cost_usd_quoted=0.06),
        rec("submitted", "r1", 2),
    ]
    assert summarize_records(approved_higher)["spent_usd_quoted"] == 0.06
    # The same fold with the records in the other order: the approved figure is chosen by kind, not by position.
    assert (
        summarize_records([approved_higher[1], approved_higher[0], approved_higher[2]])["spent_usd_quoted"]
        == 0.06
    )
    no_approval = [rec("quoted", "r2", 0, cost_usd_quoted=0.04), rec("submitted", "r2", 1)]
    assert summarize_records(no_approval)["spent_usd_quoted"] == 0.04
    assert fold_runs(approved_higher)["r1"].quoted_usd == Decimal("0.06")
    assert fold_runs(no_approval)["r2"].quoted_usd == Decimal("0.04")


def test_the_lock_timeout_default_is_unchanged(led: Ledger):
    assert led.lock_timeout_s == 5.0
