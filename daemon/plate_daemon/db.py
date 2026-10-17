"""SQLite connections for the project index: WAL, explicit transactions, numbered migrations.

`connect` opens in autocommit mode (`isolation_level=None`) so every write goes through `tx`, which
takes the write lock up front (`BEGIN IMMEDIATE`) and nests with savepoints; a `SimulatedCrash` (a
BaseException) inside `tx` rolls back exactly as a dead process would. Connections are opened per
request or per worker step, never shared across threads concurrently. The file must be on a local disk
(`config.ensure_local`): WAL is unsafe on SMB.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import ensure_local
from .migrations import MIGRATIONS
from .timeutil import iso, utcnow


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    ensure_local(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction; nested calls become savepoints of the outer transaction."""
    if conn.in_transaction:
        name = f"sp_{uuid.uuid4().hex[:10]}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_utc TEXT NOT NULL)"
    )
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in order; returns the versions applied now."""
    done = []
    applied_versions(conn)
    for version, statements in MIGRATIONS:
        with tx(conn):
            # Re-check inside the write lock: another connection may have migrated meanwhile.
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
                continue
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_utc) VALUES (?, ?)", (version, iso(utcnow()))
            )
            done.append(version)
    return done


def open_index(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def loads(s: str | None) -> Any:
    return None if s is None else json.loads(s)
